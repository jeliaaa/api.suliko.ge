"""Importing a bureau's own spreadsheet: clients and translators.

Pure code, no database: reading a file, guessing which column is which, cleaning
what is typed in a cell, and judging each row. The endpoints in
``api/v1/imports.py`` feed it what already exists and save what it accepts.

A row is one of three things:

- ``ok``: will be created (it may carry warnings, such as an email that was not
  valid and was left out).
- ``duplicate``: already in the bureau, or earlier in the same file, matched by
  phone, email or ID number. Skipped, with the existing record named.
- ``error``: cannot be created, with the reason. Skipped.

Nothing here raises for a bad row: a messy sheet is the normal case.
"""

# ruff: noqa: RUF001  (Georgian, Russian and typographic characters are what people type)

from __future__ import annotations

import csv
import io
import re
import unicodedata
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from suliko.core.errors import ValidationError
from suliko.domain.cities import CITIES

#: The size of file and number of rows one import takes. The app sits behind a
#: host that refuses bodies above about 4.5 MB, so the byte limit is below it.
MAX_BYTES = 4 * 1024 * 1024
MAX_ROWS = 5000

Kind = Literal["clients", "translators", "orders"]
KINDS: tuple[Kind, ...] = ("clients", "translators", "orders")


# ── The fields of each kind ─────────────────────────────────────────────────


@dataclass(frozen=True)
class FieldSpec:
    key: str
    required: bool = False


FIELDS: dict[str, tuple[FieldSpec, ...]] = {
    "clients": (
        FieldSpec("name", required=True),
        FieldSpec("client_type"),
        FieldSpec("phone"),
        FieldSpec("email"),
        FieldSpec("personal_number"),
        FieldSpec("address"),
        FieldSpec("acquisition_source"),
        FieldSpec("notes"),
    ),
    "translators": (
        FieldSpec("name", required=True),
        FieldSpec("phone"),
        FieldSpec("email"),
        FieldSpec("office_address"),
        FieldSpec("comment"),
        FieldSpec("default_rate"),
        FieldSpec("bank_iban"),
        FieldSpec("can_notarize"),
        FieldSpec("works_oral"),
        FieldSpec("cities"),
        FieldSpec("language_pairs"),
    ),
}

#: Headers people use, in Georgian, English and Russian, by field. Compared after
#: ``norm_header``, so case, spaces and punctuation do not matter.
SYNONYMS: dict[str, dict[str, tuple[str, ...]]] = {
    "clients": {
        "name": (
            "name",
            "full name",
            "fullname",
            "client",
            "client name",
            "customer",
            "სახელი",
            "სახელი გვარი",
            "გვარი სახელი",
            "კლიენტი",
            "კლიენტის სახელი",
            "სახელი და გვარი",
            "имя",
            "фио",
            "клиент",
            "заказчик",
        ),
        "client_type": ("type", "client type", "ტიპი", "კლიენტის ტიპი", "тип"),
        "phone": (
            "phone",
            "tel",
            "telephone",
            "mobile",
            "phone number",
            "ტელეფონი",
            "ტელ",
            "მობილური",
            "ტელეფონის ნომერი",
            "телефон",
            "тел",
        ),
        "email": ("email", "e-mail", "mail", "ელფოსტა", "ელ ფოსტა", "იმეილი", "почта"),
        "personal_number": (
            "id",
            "id number",
            "personal number",
            "personal id",
            "tax id",
            "identification code",
            "პირადი ნომერი",
            "პირადობის ნომერი",
            "საიდენტიფიკაციო კოდი",
            "ს კ",
            "პ ნ",
        ),
        "address": ("address", "მისამართი", "адрес"),
        "acquisition_source": ("source", "how did you hear", "წყარო", "источник"),
        "notes": (
            "notes",
            "note",
            "comment",
            "comments",
            "შენიშვნა",
            "კომენტარი",
            "примечание",
            "комментарий",
        ),
    },
    "translators": {
        "name": (
            "name",
            "full name",
            "translator",
            "თარჯიმანი",
            "სახელი",
            "სახელი გვარი",
            "გვარი სახელი",
            "переводчик",
            "имя",
            "фио",
        ),
        "phone": (
            "phone",
            "tel",
            "telephone",
            "mobile",
            "ტელეფონი",
            "ტელ",
            "მობილური",
            "телефон",
        ),
        "email": ("email", "e-mail", "mail", "ელფოსტა", "ელ ფოსტა", "იმეილი", "почта"),
        "office_address": ("address", "office address", "მისამართი", "адрес"),
        "comment": ("comment", "comments", "notes", "note", "შენიშვნა", "კომენტარი"),
        "default_rate": (
            "rate",
            "price per page",
            "default rate",
            "ტარიფი",
            "ფასი",
            "ფასი გვერდზე",
            "тариф",
        ),
        "bank_iban": ("iban", "account", "bank account", "ანგარიშის ნომერი", "ანგარიში"),
        "can_notarize": (
            "notary",
            "notarize",
            "notarise",
            "can notarize",
            "ნოტარიუსი",
            "ნოტარიული",
            "ნოტარიულად",
            "нотариус",
        ),
        "works_oral": (
            "oral",
            "interpreter",
            "interpreting",
            "ზეპირი",
            "ზეპირი თარგმანი",
            "устный",
        ),
        "cities": ("city", "cities", "ქალაქი", "ქალაქები", "город"),
        "language_pairs": (
            "languages",
            "language",
            "language pairs",
            "pairs",
            "ენები",
            "ენა",
            "მიმართულებები",
            "языки",
        ),
    },
}


FIELDS["orders"] = (
    FieldSpec("client_name"),
    FieldSpec("client_phone"),
    FieldSpec("client_email"),
    FieldSpec("client_personal_number"),
    FieldSpec("order_date", required=True),
    FieldSpec("due_date"),
    FieldSpec("language_pair"),
    FieldSpec("source_language"),
    FieldSpec("target_language"),
    FieldSpec("document_type"),
    FieldSpec("pages"),
    FieldSpec("price", required=True),
    FieldSpec("translator"),
    FieldSpec("translator_cost"),
    FieldSpec("status"),
    FieldSpec("paid"),
    FieldSpec("identifier"),
    FieldSpec("notes"),
)

SYNONYMS["orders"] = {
    "client_name": (
        "client",
        "client name",
        "customer",
        "name",
        "კლიენტი",
        "კლიენტის სახელი",
        "დამკვეთი",
        "სახელი გვარი",
        "клиент",
        "заказчик",
    ),
    "client_phone": (
        "phone",
        "tel",
        "mobile",
        "client phone",
        "ტელეფონი",
        "ტელ",
        "მობილური",
        "телефон",
    ),
    "client_email": ("email", "e-mail", "client email", "ელფოსტა", "იმეილი", "почта"),
    "client_personal_number": (
        "id",
        "id number",
        "personal number",
        "პირადი ნომერი",
        "პირადობის ნომერი",
        "საიდენტიფიკაციო კოდი",
        "ს კ",
    ),
    "order_date": (
        "date",
        "order date",
        "created",
        "received",
        "თარიღი",
        "შეკვეთის თარიღი",
        "მიღების თარიღი",
        "дата",
    ),
    "due_date": (
        "due",
        "due date",
        "deadline",
        "ვადა",
        "ბოლო ვადა",
        "გადაცემის თარიღი",
        "срок",
    ),
    "language_pair": (
        "languages",
        "language pair",
        "pair",
        "ენები",
        "ენობრივი წყვილი",
        "მიმართულება",
        "языки",
    ),
    "source_language": ("from", "source", "source language", "საიდან", "ორიგინალის ენა", "с"),
    "target_language": ("to", "target", "target language", "სად", "თარგმანის ენა", "на"),
    "document_type": (
        "document",
        "document type",
        "type",
        "doc type",
        "დოკუმენტი",
        "დოკუმენტის ტიპი",
        "ტიპი",
        "документ",
    ),
    "pages": (
        "pages",
        "page count",
        "qty",
        "quantity",
        "გვერდი",
        "გვერდები",
        "გვერდების რაოდენობა",
        "страниц",
    ),
    "price": (
        "price",
        "amount",
        "total",
        "cost",
        "sum",
        "ფასი",
        "თანხა",
        "ღირებულება",
        "ჯამი",
        "цена",
        "сумма",
    ),
    "translator": ("translator", "თარჯიმანი", "переводчик"),
    "translator_cost": (
        "translator cost",
        "translator fee",
        "translator payment",
        "თარჯიმნის ანაზღაურება",
        "თარჯიმნის თანხა",
    ),
    "status": ("status", "სტატუსი", "статус"),
    "paid": ("paid", "paid amount", "payment", "გადახდილი", "გადახდილი თანხა", "оплачено"),
    "identifier": (
        "identifier",
        "tender",
        "tender number",
        "reference",
        "ref",
        "იდენტიფიკატორი",
        "ტენდერი",
        "ტენდერის ნომერი",
    ),
    "notes": ("notes", "note", "comment", "comments", "შენიშვნა", "კომენტარი", "примечание"),
}


def norm_header(text: str) -> str:
    """Lower case, letters and digits only, single spaces."""
    lowered = unicodedata.normalize("NFKC", str(text)).casefold()
    cleaned = re.sub(r"[^\w]+", " ", lowered, flags=re.UNICODE).replace("_", " ")
    return " ".join(cleaned.split())


def suggest_mapping(kind: str, headers: list[str]) -> dict[str, int]:
    """Which column holds which field, guessed from the headers.

    Exact synonym first, then a header that contains one. A column serves one
    field only, and a field takes its first matching column.
    """
    wanted = SYNONYMS[kind]
    normalised = [norm_header(h) for h in headers]
    taken: set[int] = set()
    found: dict[str, int] = {}

    for exact in (True, False):
        for spec in FIELDS[kind]:
            if spec.key in found:
                continue
            words = {norm_header(w) for w in wanted.get(spec.key, ())}
            for index, header in enumerate(normalised):
                if index in taken or not header:
                    continue
                hit = header in words if exact else any(len(w) > 3 and w in header for w in words)
                if hit:
                    found[spec.key] = index
                    taken.add(index)
                    break
    return found


# ── Reading the file ────────────────────────────────────────────────────────


@dataclass
class Sheet:
    headers: list[str]
    #: (spreadsheet row number, cells) for each non-empty row after the header.
    rows: list[tuple[int, list[Any]]]


def read_sheet(content: bytes, filename: str) -> Sheet:
    """The first sheet of an .xlsx, or a .csv, as a header and rows."""
    if len(content) > MAX_BYTES:
        raise ValidationError(
            f"The file is larger than {MAX_BYTES // (1024 * 1024)} MB. "
            "Split it and import it in parts."
        )
    if not content:
        raise ValidationError("The file is empty.")

    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")) or zipfile.is_zipfile(io.BytesIO(content)):
        raw = _read_xlsx(content)
    elif name.endswith((".csv", ".txt")) or not name.endswith((".xls", ".ods")):
        raw = _read_csv(content)
    else:
        raise ValidationError(
            "Save the file as .xlsx or .csv first: the older .xls and .ods formats are not read."
        )

    numbered = [(n, row) for n, row in raw if any(cell_text(c) for c in row)]
    if not numbered:
        raise ValidationError("The file has no rows.")
    (_, header_cells), *body = numbered
    headers = [cell_text(c) for c in header_cells]
    if len(body) > MAX_ROWS:
        raise ValidationError(
            f"The file has {len(body):,} rows; one import takes at most {MAX_ROWS:,}. Split it."
        )
    if not body:
        raise ValidationError("The file has a header but no rows under it.")
    return Sheet(headers=headers, rows=body)


def _read_xlsx(content: bytes) -> list[tuple[int, list[Any]]]:
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises many kinds on a bad file
        raise ValidationError("That file could not be read as an Excel workbook.") from exc
    try:
        sheet = workbook.worksheets[0]
        return [
            (number, list(row))
            for number, row in enumerate(sheet.iter_rows(values_only=True), start=1)
        ]
    finally:
        workbook.close()


def _read_csv(content: bytes) -> list[tuple[int, list[Any]]]:
    text = _decode(content)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    return [(number, list(row)) for number, row in enumerate(reader, start=1)]


def _decode(content: bytes) -> str:
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        return content.decode("utf-16")
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("latin-1")


# ── Cleaning a cell ─────────────────────────────────────────────────────────


def cell_text(value: Any) -> str:
    """A cell as text. A whole number Excel stored as a float ("555123456.0") is not."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return " ".join(str(value).replace(" ", " ").split())


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_COMPANY_WORDS = {
    "b2b",
    "company",
    "organisation",
    "organization",
    "org",
    "legal",
    "business",
    "კომპანია",
    "ორგანიზაცია",
    "იურიდიული",
    "იურიდიული პირი",
    "ბიზნესი",
    "юрлицо",
}
_PERSON_WORDS = {
    "b2c",
    "person",
    "individual",
    "private",
    "physical",
    "ფიზიკური",
    "ფიზიკური პირი",
    "პირი",
}
_COMPANY_MARKS = re.compile(r"\b(llc|ltd|inc|gmbh|jsc|შპს|სს|ააიპ|ი/მ|ип|ооо)\b", re.IGNORECASE)
_YES = {"yes", "y", "true", "1", "x", "+", "კი", "დიახ", "да", "✓", "✔"}
_NO = {"no", "n", "false", "0", "-", "არა", "нет", ""}


def clean_phone(value: str) -> str | None:
    """As typed, spaces tidied; None when nothing that looks like a number is left."""
    digits = re.sub(r"\D", "", value)
    return value[:50] if len(digits) >= 6 else None


def phone_key(value: str | None) -> str | None:
    """The last nine digits, so "+995 555 12-34-56" and "555123456" are one number."""
    digits = re.sub(r"\D", "", value or "")
    return digits[-9:] if len(digits) >= 6 else None


def parse_bool(value: str) -> bool | None:
    """True, False, or None for something that is neither (and so is left unset)."""
    token = value.casefold().strip()
    if token in _YES:
        return True
    if token in _NO:
        return False
    return None


def parse_rate(value: str) -> Decimal | None:
    """ "25,50" and "25.5 GEL" as 25.50; None when there is no amount."""
    cleaned = re.sub(r"[^\d,.\-]", "", value).replace(",", ".")
    if not cleaned or cleaned.count(".") > 1:
        return None
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return None
    return amount if Decimal(0) <= amount <= Decimal(100000) else None


_DATE_FORMATS = (
    "%d.%m.%Y",
    "%d.%m.%y",
    "%d/%m/%Y",
    "%d/%m/%y",
    "%d-%m-%Y",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d.%m.%Y %H:%M",
    "%Y-%m-%d %H:%M:%S",
)


def parse_date(value: Any) -> date | None:
    """A date from an Excel date cell or from text such as "25.10.2025" or "2025-10-25".

    Day first, as Georgian sheets are written. None for anything else, and for
    years outside 2000 to 2100, which are a typo or a serial number.
    """
    if isinstance(value, datetime):
        found: date | None = value.date()
    elif isinstance(value, date):
        found = value
    else:
        text = cell_text(value)
        found = None
        for pattern in _DATE_FORMATS:
            try:
                found = datetime.strptime(text, pattern).date()  # noqa: DTZ007 (a calendar date, no zone)
                break
            except ValueError:
                continue
    return found if found and 2000 <= found.year <= 2100 else None


def parse_int(value: str) -> int | None:
    """ "3", "3.0" and "3 pages" as 3; None when there is no whole number."""
    match = re.search(r"\d+", value.replace(",", "."))
    return int(match.group()) if match else None


# ── Judging rows ────────────────────────────────────────────────────────────


@dataclass
class RowResult:
    row: int
    status: Literal["ok", "duplicate", "error"]
    #: What is shown for the row in the preview: its name, and what will be saved.
    name: str = ""
    values: dict[str, Any] = field(default_factory=dict)
    #: Why it is a duplicate or an error.
    messages: list[str] = field(default_factory=list)
    #: Things left out of an otherwise good row.
    warnings: list[str] = field(default_factory=list)
    #: The existing record a duplicate matched, as "number name".
    matched: str | None = None


@dataclass
class ExistingPerson:
    """One record already in the bureau, for duplicate matching."""

    label: str
    phone: str | None = None
    email: str | None = None
    personal_number: str | None = None
    name: str | None = None
    #: What the record is called elsewhere: a client's id, or a placeholder for one
    #: this import is about to create.
    ref: int | str | None = None


class Seen:
    """Who is already known: the bureau's records, then the rows accepted so far."""

    def __init__(self, existing: Iterable[ExistingPerson] = ()) -> None:
        # Each maps to (label, ref).
        self.by_phone: dict[str, tuple[str, int | str | None]] = {}
        self.by_email: dict[str, tuple[str, int | str | None]] = {}
        self.by_number: dict[str, tuple[str, int | str | None]] = {}
        self.by_name: dict[str, tuple[str, int | str | None]] = {}
        for person in existing:
            self.add(person)

    def add(self, person: ExistingPerson) -> None:
        entry = (person.label, person.ref)
        if key := phone_key(person.phone):
            self.by_phone.setdefault(key, entry)
        if person.email:
            self.by_email.setdefault(person.email.casefold(), entry)
        if person.personal_number:
            self.by_number.setdefault(person.personal_number.casefold(), entry)
        if person.name:
            self.by_name.setdefault(person.name.casefold(), entry)

    def match_ref(
        self, person: ExistingPerson, *, by_name: bool = False
    ) -> tuple[str, str, int | str | None] | None:
        """(what matched, the record's label, its ref), or None."""
        if (key := phone_key(person.phone)) and key in self.by_phone:
            return ("phone", *self.by_phone[key])
        if person.email and person.email.casefold() in self.by_email:
            return ("email", *self.by_email[person.email.casefold()])
        if person.personal_number and person.personal_number.casefold() in self.by_number:
            return ("ID number", *self.by_number[person.personal_number.casefold()])
        if by_name and person.name and person.name.casefold() in self.by_name:
            return ("name", *self.by_name[person.name.casefold()])
        return None

    def match(self, person: ExistingPerson, *, by_name: bool = False) -> tuple[str, str] | None:
        """(what matched, the record's label), or None."""
        hit = self.match_ref(person, by_name=by_name)
        return (hit[0], hit[1]) if hit else None


def _cell(cells: list[Any], mapping: dict[str, int], key: str) -> str:
    index = mapping.get(key)
    if index is None or index >= len(cells):
        return ""
    return cell_text(cells[index])


def _common(
    cells: list[Any], mapping: dict[str, int], result: RowResult
) -> tuple[str, str | None, str | None]:
    """Name, phone and email, cleaned, with what was left out noted."""
    name = _cell(cells, mapping, "name")
    phone_raw = _cell(cells, mapping, "phone")
    email_raw = _cell(cells, mapping, "email").casefold()

    phone = clean_phone(phone_raw) if phone_raw else None
    if phone_raw and phone is None:
        result.warnings.append(f"Phone left out: {phone_raw[:30]!r} is not a number.")
    email: str | None = None
    if email_raw:
        if _EMAIL.match(email_raw) and len(email_raw) <= 255:
            email = email_raw
        else:
            result.warnings.append(f"Email left out: {email_raw[:40]!r} is not valid.")
    return name, phone, email


def judge_client(number: int, cells: list[Any], mapping: dict[str, int], seen: Seen) -> RowResult:
    result = RowResult(row=number, status="ok")
    name, phone, email = _common(cells, mapping, result)
    result.name = name

    if not name:
        result.status = "error"
        result.messages.append("No name.")
        return result
    if len(name) > 255:
        result.status = "error"
        result.messages.append("The name is longer than 255 characters.")
        return result

    kind_text = _cell(cells, mapping, "client_type").casefold()
    if kind_text in _COMPANY_WORDS:
        client_type = "B2B"
    elif kind_text in _PERSON_WORDS:
        client_type = "B2C"
    else:
        client_type = "B2B" if _COMPANY_MARKS.search(name) else "B2C"

    personal = _cell(cells, mapping, "personal_number")[:50] or None
    address = _cell(cells, mapping, "address")[:500] or None
    source = _cell(cells, mapping, "acquisition_source")[:255] or None
    notes = _cell(cells, mapping, "notes") or None

    person = ExistingPerson(
        label=name, phone=phone, email=email, personal_number=personal, name=None
    )
    hit = seen.match(person)
    if hit:
        result.status = "duplicate"
        result.matched = hit[1]
        result.messages.append(f"Already in the list by {hit[0]}: {hit[1]}.")
        return result

    seen.add(person)
    result.values = {
        "name": name,
        "client_type": client_type,
        "phone": phone,
        "email": email,
        "personal_number": personal,
        "address": address,
        "acquisition_source": source,
        "notes": notes,
    }
    return result


# ── Translators ─────────────────────────────────────────────────────────────

CITY_NAMES: dict[str, tuple[str, str]] = {
    "tbilisi": ("Tbilisi", "თბილისი"),
    "batumi": ("Batumi", "ბათუმი"),
    "kutaisi": ("Kutaisi", "ქუთაისი"),
    "rustavi": ("Rustavi", "რუსთავი"),
    "gori": ("Gori", "გორი"),
    "zugdidi": ("Zugdidi", "ზუგდიდი"),
    "poti": ("Poti", "ფოთი"),
    "telavi": ("Telavi", "თელავი"),
    "akhaltsikhe": ("Akhaltsikhe", "ახალციხე"),
    "ozurgeti": ("Ozurgeti", "ოზურგეთი"),
    "kobuleti": ("Kobuleti", "ქობულეთი"),
    "mtskheta": ("Mtskheta", "მცხეთა"),
    "sighnaghi": ("Sighnaghi", "სიღნაღი"),
    "borjomi": ("Borjomi", "ბორჯომი"),
    "marneuli": ("Marneuli", "მარნეული"),
    "gardabani": ("Gardabani", "გარდაბანი"),
    "samtredia": ("Samtredia", "სამტრედია"),
    "senaki": ("Senaki", "სენაკი"),
    "khashuri": ("Khashuri", "ხაშური"),
    "sachkhere": ("Sachkhere", "საჩხერე"),
}
assert set(CITY_NAMES) == set(CITIES), "CITY_NAMES must list every city in domain.cities"

_SPLIT = re.compile(r"[,;/\n|]+")
_DASH = re.compile(r"\s*(?:->|→|>|–|—|-|=>)\s*")


def parse_cities(value: str) -> tuple[list[str], list[str]]:
    """City codes from "Tbilisi, ბათუმი", and the words that were not a known city."""
    lookup: dict[str, str] = {}
    for code, (en, ka) in CITY_NAMES.items():
        lookup[code] = code
        lookup[en.casefold()] = code
        lookup[ka.casefold()] = code
    codes: list[str] = []
    unknown: list[str] = []
    for part in _SPLIT.split(value):
        word = part.strip().casefold()
        if not word:
            continue
        code = lookup.get(word)
        if code is None:
            unknown.append(part.strip())
        elif code not in codes:
            codes.append(code)
    return codes, unknown


def parse_pairs(
    value: str, language_lookup: Callable[[str], str | None]
) -> tuple[list[tuple[str, str]], list[str]]:
    """Language pairs from "ka-en; English > Georgian", and the pieces not understood.

    ``language_lookup`` turns a code or a name (in any language) into the
    bureau's language code, or None.
    """
    pairs: list[tuple[str, str]] = []
    problems: list[str] = []
    for part in _SPLIT.split(value):
        text = part.strip()
        if not text:
            continue
        sides = _DASH.split(text)
        if len(sides) != 2:
            problems.append(text)
            continue
        source, target = (language_lookup(sides[0].strip()), language_lookup(sides[1].strip()))
        if source is None or target is None or source == target:
            problems.append(text)
            continue
        if (source, target) not in pairs:
            pairs.append((source, target))
    return pairs, problems


def judge_translator(
    number: int,
    cells: list[Any],
    mapping: dict[str, int],
    seen: Seen,
    language_lookup: Callable[[str], str | None],
) -> RowResult:
    result = RowResult(row=number, status="ok")
    name, phone, email = _common(cells, mapping, result)
    result.name = name

    if not name:
        result.status = "error"
        result.messages.append("No name.")
        return result
    if len(name) > 255:
        result.status = "error"
        result.messages.append("The name is longer than 255 characters.")
        return result

    rate_text = _cell(cells, mapping, "default_rate")
    rate = parse_rate(rate_text) if rate_text else None
    if rate_text and rate is None:
        result.warnings.append(f"Rate left out: {rate_text[:20]!r} is not an amount.")

    notarize = parse_bool(_cell(cells, mapping, "can_notarize"))
    oral = parse_bool(_cell(cells, mapping, "works_oral"))

    cities, unknown_cities = parse_cities(_cell(cells, mapping, "cities"))
    if unknown_cities:
        result.warnings.append("Cities not recognised: " + ", ".join(unknown_cities[:5]) + ".")

    pairs, pair_problems = parse_pairs(_cell(cells, mapping, "language_pairs"), language_lookup)
    if pair_problems:
        result.warnings.append(
            "Language pairs not understood (use ka-en, or English-Georgian): "
            + "; ".join(pair_problems[:5])
            + "."
        )

    person = ExistingPerson(label=name, phone=phone, email=email, name=name)
    hit = seen.match(person, by_name=True)
    if hit:
        result.status = "duplicate"
        result.matched = hit[1]
        result.messages.append(f"Already in the list by {hit[0]}: {hit[1]}.")
        return result

    seen.add(person)
    result.values = {
        "name": name,
        "phone": phone,
        "email": email,
        "office_address": _cell(cells, mapping, "office_address")[:255] or None,
        "comment": _cell(cells, mapping, "comment") or None,
        "default_rate": rate,
        "bank_iban": _cell(cells, mapping, "bank_iban").replace(" ", "")[:34] or None,
        "can_notarize": bool(notarize),
        "works_oral": bool(oral),
        "cities": cities,
        "language_pairs": pairs,
    }
    return result


# ── The whole file ──────────────────────────────────────────────────────────


def check_mapping(kind: str, mapping: dict[str, int], column_count: int) -> dict[str, int]:
    """The mapping, or a ValidationError saying what is wrong with it."""
    allowed = {spec.key for spec in FIELDS[kind]}
    clean: dict[str, int] = {}
    for key, index in mapping.items():
        if key not in allowed:
            raise ValidationError(f"Unknown field: {key}.")
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < column_count:
            raise ValidationError(f"Column for {key} is not in the file.")
        clean[key] = index
    for spec in FIELDS[kind]:
        if spec.required and spec.key not in clean:
            raise ValidationError(f"Choose the column for {spec.key.replace('_', ' ')}.")
    if len(set(clean.values())) != len(clean):
        raise ValidationError("A column can be used for one field only.")
    return clean


@dataclass
class Judged:
    results: list[RowResult]

    def count(self, status: str) -> int:
        return sum(1 for r in self.results if r.status == status)


def judge_sheet(
    kind: str,
    sheet: Sheet,
    mapping: dict[str, int],
    existing: Iterable[ExistingPerson],
    language_lookup: Callable[[str], str | None] | None = None,
    orders: Any = None,
) -> Judged:
    seen = Seen(existing)
    results: list[RowResult] = []
    if kind == "orders":
        from suliko.domain.imports_orders import judge_orders

        return Judged(judge_orders(sheet, mapping, seen, orders))
    for number, cells in sheet.rows:
        if kind == "clients":
            results.append(judge_client(number, cells, mapping, seen))
        else:
            results.append(
                judge_translator(number, cells, mapping, seen, language_lookup or (lambda _t: None))
            )
    return Judged(results)
