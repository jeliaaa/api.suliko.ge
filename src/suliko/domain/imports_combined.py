"""One spreadsheet, several kinds of record.

A bureau's own file is rarely tidy. It may be a workbook with a sheet per kind
(orders, clients, translators, notaries) and a cover sheet; it may have a title
above the header and month banners between the rows; or it may be one flat sheet
with a client, a translator and a notary on every order. Nothing here touches the
database. It reads, decides, and hands every decision back as something the person
can see and change before anything is saved.

1. ``plan_sheets`` finds each sheet's header row, says what the sheet is and how
   sure it is, and sets aside the rows that are not records: headings, month
   banners, totals, footnotes.
2. ``judge_workbook`` judges the rows of every sheet that is a kind, in the order
   that lets a later row point at an earlier one: notaries, translators, clients,
   then orders. An order names its client, translator and notary, and finds them
   among the bureau's own, among those an earlier sheet creates, or, when the
   file has no sheet for that kind of person, among those the orders themselves
   introduce.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from suliko.core.errors import ValidationError
from suliko.domain.imports import (
    DISTINCTIVE,
    FIELDS,
    KINDS,
    MAX_ROWS,
    ExistingPerson,
    Kind,
    RawSheet,
    RowResult,
    Seen,
    Sheet,
    cell_text,
    check_mapping,
    judge_client,
    judge_notary,
    judge_translator,
    norm_header,
    suggest_mapping,
)
from suliko.domain.imports_orders import OrdersContext, judge_orders, remember_person

#: How many rows down a header is looked for. A title, a logo line and a blank
#: or two fit well inside.
HEADER_SCAN = 15

Confidence = Literal["high", "low", "none"]

#: Why a sheet was given its kind, for the person to read:
#: ``columns`` the columns say it; ``name`` the sheet's name agrees; ``unsure`` more
#: than one kind fits and this is the best of them; ``nothing`` no columns were
#: recognised (a cover sheet, notes); ``chosen`` the person chose.
Why = Literal["columns", "name", "unsure", "nothing", "chosen"]

#: Words in a sheet's name that say what it holds, after ``norm_header``.
NAME_HINTS: dict[str, tuple[str, ...]] = {
    "clients": ("client", "customer", "კლიენტ", "დამკვეთ", "клиент", "заказчик"),
    "translators": ("translator", "თარჯიმ", "переводчик"),
    "notaries": ("notar", "ნოტარ", "нотариус"),
    "orders": ("order", "შეკვეთ", "заказ"),
}

#: A cell with just one of these makes its row a total, not a record.
TOTAL_WORDS = frozenset(
    {
        "total",
        "totals",
        "subtotal",
        "grand total",
        "sum",
        "ჯამი",
        "ჯამური",
        "სულ",
        "ჯამში",
        "итого",
        "всего",
        "сумма",
    }
)

SkipReason = Literal["heading", "totals"]


@dataclass
class Choice:
    """What the person changed about one sheet. Whatever is left None is worked out."""

    skip: bool = False
    kind: Kind | None = None
    header_row: int | None = None
    mapping: dict[str, int] | None = None


@dataclass
class SheetPlan:
    index: int
    name: str
    #: The spreadsheet row of the header, or None for a sheet with no header.
    header_row: int | None
    headers: list[str]
    #: None: this sheet is left out.
    kind: Kind | None
    confidence: Confidence
    why: Why
    #: Field -> column, for the kind.
    mapping: dict[str, int]
    #: The rows that are records, each with its spreadsheet row number.
    rows: list[tuple[int, list[Any]]]
    #: Rows after the header that are not records, and why.
    skipped: list[tuple[int, SkipReason]] = field(default_factory=list)


# ── Finding the header ──────────────────────────────────────────────────────


def _texts(cells: list[Any]) -> list[str]:
    return [cell_text(c) for c in cells]


def find_header(rows: list[tuple[int, list[Any]]]) -> tuple[int, bool] | None:
    """Where in ``rows`` the header is, and whether its columns were recognised.

    The recognised header is the row, among the first few, that names the most
    fields. It names at least two fields of some kind, so a title (one cell) and a
    row of data never pass; the earliest wins a tie. A sheet whose columns are
    nothing we know still has a header: the first row with two filled cells, so the
    person can say what the sheet holds and which column is which.
    """
    found: int | None = None
    best = 1
    first_wide: int | None = None
    for position, (_, cells) in enumerate(rows[:HEADER_SCAN]):
        texts = _texts(cells)
        if sum(1 for text in texts if text) < 2:
            continue
        if first_wide is None:
            first_wide = position
        score = max(len(suggest_mapping(kind, texts)) for kind in KINDS)
        if score > best:
            found, best = position, score
    if found is not None:
        return found, True
    return (first_wide, False) if first_wide is not None else None


# ── Saying what a sheet is ──────────────────────────────────────────────────


def classify(
    sheet_name: str, headers: list[str]
) -> tuple[Kind | None, Confidence, Why, dict[str, int]]:
    """The kind a sheet most likely is, how sure that is, why, and its column mapping.

    A kind is possible only when its required columns are there. Of those, the one
    whose own columns (a registration number for a notary, a rate for a translator,
    a price for an order) are most present wins, with the sheet's name as a nudge.
    """
    folded = norm_header(sheet_name)
    scored: list[tuple[int, Kind, dict[str, int], bool]] = []
    for kind in KINDS:
        mapping = suggest_mapping(kind, headers)
        if any(spec.required and spec.key not in mapping for spec in FIELDS[kind]):
            continue
        distinctive = len(DISTINCTIVE[kind] & mapping.keys())
        hinted = any(word in folded for word in NAME_HINTS[kind])
        scored.append(
            (3 * distinctive + len(mapping) + (4 if hinted else 0), kind, mapping, hinted)
        )
    if not scored:
        return None, "none", "nothing", {}
    scored.sort(key=lambda entry: entry[0], reverse=True)
    score, kind, mapping, hinted = scored[0]
    gap = score - scored[1][0] if len(scored) > 1 else score
    distinctive_hits = len(DISTINCTIVE[kind] & mapping.keys())
    if distinctive_hits == 0 and not hinted:
        # Only a name and perhaps a phone: it could be a list of anyone.
        return kind, "low", "unsure", mapping
    if gap >= 3:
        return kind, "high", "name" if hinted else "columns", mapping
    return kind, "low", "unsure", mapping


# ── Setting aside the rows that are not records ─────────────────────────────


def split_rows(
    kind: Kind, mapping: dict[str, int], rows: list[tuple[int, list[Any]]]
) -> tuple[list[tuple[int, list[Any]]], list[tuple[int, SkipReason]]]:
    """Records, and the rows that only look like them.

    A row is a total when one of its cells is just a word like "Total". It is a
    heading (a month, a footnote) when only one cell is filled and the row could not
    be a record anyway: any single-cell row in an orders sheet (an order needs a date,
    languages and a price), and in other sheets a single cell outside every mapped
    column, such as a row counter. A single cell in the name column is a person with
    only a name, and stays.
    """
    mapped = set(mapping.values())
    kept: list[tuple[int, list[Any]]] = []
    skipped: list[tuple[int, SkipReason]] = []
    for number, cells in rows:
        filled = [i for i, cell in enumerate(cells) if cell_text(cell)]
        if any(norm_header(cell_text(cells[i])) in TOTAL_WORDS for i in filled):
            skipped.append((number, "totals"))
        elif len(filled) == 1 and (kind == "orders" or filled[0] not in mapped):
            skipped.append((number, "heading"))
        else:
            kept.append((number, cells))
    return kept, skipped


# ── The plan ────────────────────────────────────────────────────────────────


def _plan_one(index: int, raw: RawSheet, choice: Choice) -> SheetPlan:
    rows = raw.rows
    if choice.header_row is not None:
        position = next((i for i, (n, _) in enumerate(rows) if n == choice.header_row), None)
        if position is None:
            raise ValidationError(
                f"Sheet {raw.name!r}: row {choice.header_row} is empty or not in the sheet."
            )
        recognised = True
    else:
        found = find_header(rows)
        position, recognised = found if found is not None else (None, False)
    if position is None:
        return SheetPlan(index, raw.name, None, [], None, "none", "nothing", {}, [])

    header_row, header_cells = rows[position]
    headers = _texts(header_cells)
    body = rows[position + 1 :]

    kind: Kind | None
    confidence: Confidence
    why: Why
    if choice.skip:
        kind, confidence, why, mapping = None, "none", "chosen", {}
    elif choice.kind is not None:
        kind, confidence, why = choice.kind, "high", "chosen"
        mapping = suggest_mapping(kind, headers)
    elif not recognised:
        kind, confidence, why, mapping = None, "none", "nothing", {}
    else:
        kind, confidence, why, mapping = classify(raw.name, headers)
    if kind is not None and choice.mapping is not None:
        mapping = choice.mapping

    kept, skipped = (body, []) if kind is None else split_rows(kind, mapping, body)
    return SheetPlan(
        index, raw.name, header_row, headers, kind, confidence, why, mapping, kept, skipped
    )


def plan_sheets(
    sheets: list[RawSheet], choices: dict[int, Choice] | None = None
) -> list[SheetPlan]:
    """A plan for every sheet: what it is, where its header is, which columns are which."""
    choices = choices or {}
    return [_plan_one(i, sheet, choices.get(i, Choice())) for i, sheet in enumerate(sheets)]


# ── Judging all of it ───────────────────────────────────────────────────────


@dataclass
class Directory:
    """What the bureau already has, for matching."""

    clients: list[ExistingPerson] = field(default_factory=list)
    translators: list[ExistingPerson] = field(default_factory=list)
    notaries: list[ExistingPerson] = field(default_factory=list)


@dataclass
class SheetResult:
    plan: SheetPlan
    results: list[RowResult]

    def count(self, status: str) -> int:
        return sum(1 for r in self.results if r.status == status)


@dataclass
class WorkbookJudged:
    #: In the order of the file's sheets.
    sheets: list[SheetResult]
    #: Holds the translators and notaries the orders introduce, once judged.
    ctx: OrdersContext

    def ok(self, kind: str) -> list[RowResult]:
        return [
            r for s in self.sheets if s.plan.kind == kind for r in s.results if r.status == "ok"
        ]

    def count(self, status: str) -> int:
        return sum(s.count(status) for s in self.sheets)

    @property
    def total(self) -> int:
        return sum(len(s.results) for s in self.sheets)


def _judge_people(
    plan: SheetPlan,
    seen: Seen,
    ctx: OrdersContext,
    language_lookup: Callable[[str], str | None],
) -> list[RowResult]:
    """A sheet of translators or notaries, remembered as they are judged."""
    notaries = plan.kind == "notaries"
    by_name = ctx.notaries if notaries else ctx.translators
    by_phone = ctx.notaries_by_phone if notaries else ctx.translators_by_phone
    results: list[RowResult] = []
    for number, cells in plan.rows:
        if notaries:
            result = judge_notary(number, cells, plan.mapping, seen, ctx.notary_refs)
        else:
            result = judge_translator(
                number, cells, plan.mapping, seen, language_lookup, ctx.translator_refs
            )
        # A duplicate is the record it matched, under this spelling too.
        ref = result.ref if result.status == "ok" else result.matched_ref
        if result.status != "error":
            remember_person(by_name, by_phone, ref, result.name, result.values.get("phone"))
        results.append(result)
    return results


def judge_workbook(
    plans: list[SheetPlan],
    directory: Directory,
    ctx: OrdersContext,
    language_lookup: Callable[[str], str | None],
) -> WorkbookJudged:
    """Judge every sheet that has a kind, earlier kinds first. Saves nothing."""
    chosen = [plan for plan in plans if plan.kind is not None]
    if not chosen:
        raise ValidationError(
            "None of the sheets could be recognised. Choose what each sheet holds."
        )
    if sum(len(plan.rows) for plan in chosen) > MAX_ROWS:
        raise ValidationError(
            f"The file has more than {MAX_ROWS:,} rows; one import takes at most that. Split it."
        )
    for plan in chosen:
        assert plan.kind is not None
        try:
            plan.mapping = check_mapping(plan.kind, plan.mapping, len(plan.headers))
        except ValidationError as exc:
            raise ValidationError(f"Sheet {plan.name!r}: {exc.detail}") from exc
        if not plan.rows:
            raise ValidationError(f"Sheet {plan.name!r} has a header but no rows under it.")

    kinds = {plan.kind for plan in chosen}
    # With no sheet of translators (or notaries) the orders are all the file says about them.
    ctx.create_translators = "translators" not in kinds
    ctx.create_notaries = "notaries" not in kinds

    clients_seen = Seen(directory.clients)
    translators_seen = Seen(directory.translators)
    notaries_seen = Seen(directory.notaries)

    judged: dict[int, list[RowResult]] = {}
    for kind in ("notaries", "translators", "clients", "orders"):
        for plan in chosen:
            if plan.kind != kind:
                continue
            if kind == "notaries":
                judged[plan.index] = _judge_people(plan, notaries_seen, ctx, language_lookup)
            elif kind == "translators":
                judged[plan.index] = _judge_people(plan, translators_seen, ctx, language_lookup)
            elif kind == "clients":
                judged[plan.index] = [
                    judge_client(number, cells, plan.mapping, clients_seen, ctx.client_refs)
                    for number, cells in plan.rows
                ]
            else:
                sheet = Sheet(headers=plan.headers, rows=plan.rows)
                judged[plan.index] = judge_orders(sheet, plan.mapping, clients_seen, ctx)

    return WorkbookJudged(
        sheets=[SheetResult(plan, judged[plan.index]) for plan in chosen],
        ctx=ctx,
    )
