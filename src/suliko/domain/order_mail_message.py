"""The words of the email that carries an order's files, in plain text and HTML.

Both versions say the same thing: who sent it, which order, when it is due and
how urgent, then each document with its languages, pages and (for a client) its
kind of copy, and the files under it. What it never says: prices, the client's
contact details, or the bureau's internal notes. The sender's own message goes
in as written.

## Why the HTML is plain

Gmail files mail that looks like a campaign under Updates or Promotions, and
this is a person sending files to a person. So: one column, the system's fonts,
no images, buttons or links, one accent colour. The accent is used for the top
stripe, the organisation's name, the order card and the file-type tags. Nothing
sets a page background, so a reader's dark mode still works; the two coloured
boxes set their own text colour, so they stay readable either way.

Everything a person typed (names, file names, the note) is escaped. The
sentences around it are ours.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import PurePath
from typing import Literal, NamedTuple

Language = Literal["ka", "en"]
Audience = Literal["client", "translator"]

# Suliko's blue (the same as on suliko.ge), a darker one for text on a tint,
# and the tints. The rest is neutral.
_ACCENT = "#3b59f3"
_ACCENT_DARK = "#2339b8"
_TINT = "#eef1ff"
_TINT_LINE = "#d6dcfb"
_MUTED = "#6b7280"
_LINE = "#d4d4d8"
_INK = "#111827"
_FONT = (
    "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Noto Sans Georgian',"
    "'Helvetica Neue',Arial,sans-serif"
)

# ── The words ───────────────────────────────────────────────────────────────

_SUBJECT: dict[Language, str] = {
    "en": "Documents for order #{number} from {organisation}",
    "ka": "დოკუმენტები შეკვეთისთვის №{number}: {organisation}",
}
#: The sender's name in the inbox: the bureau, and which order this is about.
_FROM_NAME: dict[Language, str] = {
    "en": "{organisation}, order #{number}",
    "ka": "{organisation}, შეკვეთა №{number}",
}
_GREETING: dict[Language, str] = {"en": "Hello {name},", "ka": "გამარჯობა, {name}!"}
_INTRO: dict[tuple[Audience, Language], str] = {
    ("client", "en"): "{organisation} has sent you these files for order #{number}:",
    ("client", "ka"): "{organisation} გიგზავნით ამ ფაილებს შეკვეთისთვის №{number}:",
    ("translator", "en"): "{organisation} has sent you these files to work on, order #{number}:",
    ("translator", "ka"): "{organisation} გიგზავნით ამ ფაილებს სამუშაოდ, შეკვეთა №{number}:",
}
_REPLY: dict[Language, str] = {
    "en": "To get in touch, reply to this email.",
    "ka": "დასაკავშირებლად უპასუხეთ ამ წერილს.",
}
_SENT_THROUGH: dict[Language, str] = {
    "en": "Sent through Suliko.",
    "ka": "გაგზავნილია Suliko-ს მეშვეობით.",
}
_NUMBER_SIGN: dict[Language, str] = {"en": "#", "ka": "№"}
_ORDER: dict[Language, str] = {"en": "Order", "ka": "შეკვეთა"}
_DUE: dict[Language, str] = {"en": "Due date", "ka": "ვადა"}
_URGENCY: dict[Language, str] = {"en": "Urgency", "ka": "სასწრაფოობა"}
_URGENCY_NAMES: dict[tuple[str, Language], str] = {
    ("standard", "en"): "Standard",
    ("standard", "ka"): "სტანდარტული",
    ("express", "en"): "Express",
    ("express", "ka"): "ექსპრესი",
    ("urgent", "en"): "Urgent",
    ("urgent", "ka"): "სასწრაფო",
}
_COPY_NAMES: dict[tuple[str, Language], str] = {
    ("original", "en"): "Original document",
    ("original", "ka"): "ორიგინალი დოკუმენტი",
    ("plain", "en"): "Plain copy",
    ("plain", "ka"): "უბრალო ასლი",
    ("notary_original", "en"): "Notary on original",
    ("notary_original", "ka"): "ნოტარიული დამოწმება ორიგინალზე",
    ("notary_copy", "en"): "Plain copy",
    ("notary_copy", "ka"): "უბრალო ასლი",
    ("notary_certified", "en"): "Certified copy",
    ("notary_certified", "ka"): "დამოწმებული ასლი",
}
_NOTARISED: dict[Language, str] = {"en": "Notarised", "ka": "ნოტარიულად დამოწმებული"}
_UNTITLED: dict[Language, str] = {"en": "Document", "ka": "დოკუმენტი"}


def _pages(count: int, language: Language) -> str:
    if language == "ka":
        return f"{count} გვერდი"
    return f"{count} page" if count == 1 else f"{count} pages"


def _date(value: date) -> str:
    return value.strftime("%d.%m.%Y")


# ── What goes in ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MailDocument:
    """One document of the order, and the files of it that are being sent."""

    #: Its type in the email's language ("Passport"), or empty.
    title: str
    #: "English → Georgian", or empty when a language is unknown.
    route: str
    pages: int
    #: The kind of copy. Only a client is told; a translator has no use for it.
    copy: str
    files: tuple[str, ...]


class Composed(NamedTuple):
    subject: str
    text: str
    html: str


@dataclass(frozen=True, slots=True)
class _Letter:
    """Everything either version is made from."""

    to: Audience
    language: Language
    organisation: str
    order_number: int
    recipient_name: str
    due_date: date | None
    urgency: str
    documents: Sequence[MailDocument]
    note: str | None


def sender_name(*, language: Language, organisation: str, order_number: int) -> str:
    """The name the message shows in an inbox, in place of 'Bureau via Suliko'.

    The bureau is cut to 50 characters so the order number is never what gets
    cut off: mail apps clip long names, and the number is the useful part.
    """
    return _FROM_NAME[language].format(organisation=organisation[:50], number=order_number)


def copy_label(copy_type: str, is_notarized: bool, language: Language) -> str:
    """'Certified copy', 'Original document, Notarised': what the client is
    getting. Empty for a value this does not know."""
    name = _COPY_NAMES.get((copy_type, language), "")
    if is_notarized and "notary" not in copy_type:
        name = f"{name}, {_NOTARISED[language]}" if name else _NOTARISED[language]
    return name


def untitled(language: Language) -> str:
    return _UNTITLED[language]


# ── Plain text ──────────────────────────────────────────────────────────────


def _meta(document: MailDocument, language: Language) -> list[str]:
    return [
        part for part in (document.route, _pages(document.pages, language), document.copy) if part
    ]


def _facts(language: Language, due_date: date | None, urgency: str) -> list[tuple[str, str]]:
    """The order's facts as label and value, in the order they are shown."""
    facts: list[tuple[str, str]] = []
    if due_date is not None:
        facts.append((_DUE[language], _date(due_date)))
    name = _URGENCY_NAMES.get((urgency, language))
    if name:
        facts.append((_URGENCY[language], name))
    return facts


def _text(letter: _Letter) -> str:
    language = letter.language
    parts = [
        _GREETING[language].format(name=letter.recipient_name),
        _INTRO[(letter.to, language)].format(
            organisation=letter.organisation, number=letter.order_number
        ),
    ]
    facts = _facts(language, letter.due_date, letter.urgency)
    if facts:
        parts.append("\n".join(f"{label}: {value}" for label, value in facts))
    for document in letter.documents:
        head = document.title or _UNTITLED[language]
        meta = _meta(document, language)
        if meta:
            head = f"{head} ({', '.join(meta)})"
        parts.append(head + "\n" + "\n".join(f"- {name}" for name in document.files))
    if letter.note:
        parts.append(letter.note)
    parts.append(_REPLY[language])
    return "\n\n".join(parts) + "\n"


# ── HTML ────────────────────────────────────────────────────────────────────


def _esc(value: str) -> str:
    return html.escape(value, quote=True)


def _lines(value: str) -> str:
    """Text as HTML, keeping the sender's own line breaks."""
    return "<br>".join(_esc(line) for line in value.strip().splitlines())


def _urgency_tag(urgency: str, label: str) -> str:
    """Standard is just text; Express and Urgent are tags, stronger as it grows."""
    if urgency == "standard":
        return _esc(label)
    colours = (
        f"background:{_ACCENT};color:#ffffff;"
        if urgency == "urgent"
        else f"background:#dbe3ff;color:{_ACCENT_DARK};"
    )
    return (
        f'<span style="display:inline-block;padding:2px 11px;border-radius:999px;'
        f'font-size:13px;font-weight:700;{colours}">{_esc(label)}</span>'
    )


def _facts_card(language: Language, order_number: int, due_date: date | None, urgency: str) -> str:
    cells: list[tuple[str, str]] = [
        (_ORDER[language], _esc(f"{_NUMBER_SIGN[language]}{order_number}"))
    ]
    if due_date is not None:
        cells.append((_DUE[language], _esc(_date(due_date))))
    name = _URGENCY_NAMES.get((urgency, language))
    if name:
        cells.append((_URGENCY[language], _urgency_tag(urgency, name)))
    width = 100 // len(cells)
    row = "".join(
        f'<td valign="top" width="{width}%" style="padding:12px 14px;">'
        f'<div style="font-size:12px;color:#5b6478;padding:0 0 3px 0;">{_esc(label)}</div>'
        f'<div style="font-size:16px;font-weight:700;color:{_INK};">{value}</div></td>'
        for label, value in cells
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'bgcolor="{_TINT}" style="background:{_TINT};border:1px solid {_TINT_LINE};'
        f'border-radius:10px;border-collapse:separate;color:{_INK};"><tr>{row}</tr></table>'
    )


def _file_row(name: str, *, first: bool) -> str:
    kind = PurePath(name).suffix.lstrip(".").upper()[:4] or "FILE"
    return (
        f'<tr><td style="padding:9px 14px;font-size:14px;word-break:break-word;'
        f'{"" if first else f"border-top:1px solid {_LINE};"}">'
        f'<span style="display:inline-block;min-width:30px;margin:0 10px 0 0;padding:2px 6px;'
        f"border-radius:5px;background:{_TINT};color:{_ACCENT_DARK};font-size:10px;"
        f'font-weight:700;letter-spacing:.04em;text-align:center;">{_esc(kind)}</span>'
        f"{_esc(name)}</td></tr>"
    )


def _document_card(document: MailDocument, language: Language) -> str:
    meta = " &middot; ".join(_esc(part) for part in _meta(document, language))
    files = "".join(_file_row(name, first=False) for name in document.files)
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="border:1px solid {_LINE};border-radius:10px;border-collapse:separate;'
        'margin:0 0 10px 0;">'
        '<tr><td style="padding:12px 14px 10px 14px;">'
        f'<div style="font-size:15px;font-weight:700;">'
        f"{_esc(document.title or _UNTITLED[language])}</div>"
        f'<div style="font-size:13px;color:{_MUTED};padding:2px 0 0 0;">{meta}</div>'
        f"</td></tr>{files}</table>"
    )


def _html(letter: _Letter) -> str:
    language = letter.language
    organisation = letter.organisation
    order_number = letter.order_number
    note = letter.note
    greeting = _esc(_GREETING[language].format(name=letter.recipient_name))
    intro = _INTRO[(letter.to, language)].format(
        organisation=f"<strong>{_esc(organisation)}</strong>",
        number=f"<strong>{order_number}</strong>",
    )
    cards = "".join(_document_card(document, language) for document in letter.documents)
    facts = _facts_card(language, order_number, letter.due_date, letter.urgency)
    note_block = (
        f'<tr><td style="padding:8px 0 20px 0;"><div style="border-left:3px solid {_ACCENT};'
        f'padding:2px 0 2px 14px;font-size:15px;line-height:1.55;">{_lines(note)}</div>'
        "</td></tr>"
        if note
        else ""
    )
    return (
        f'<!doctype html><html lang="{language}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        '<body style="margin:0;padding:0;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        '<tr><td align="center" style="padding:24px 16px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="max-width:540px;font-family:{_FONT};font-size:15px;line-height:1.55;">'
        f'<tr><td bgcolor="{_ACCENT}" style="height:4px;font-size:0;line-height:0;'
        f'background:{_ACCENT};border-radius:4px;">&nbsp;</td></tr>'
        f'<tr><td style="padding:16px 0 14px 0;font-size:16px;font-weight:700;'
        f'color:{_ACCENT};">{_esc(organisation)}</td></tr>'
        f'<tr><td style="padding:6px 0 8px 0;">{greeting}</td></tr>'
        f'<tr><td style="padding:0 0 14px 0;">{intro}</td></tr>'
        f'<tr><td style="padding:0 0 16px 0;">{facts}</td></tr>'
        f'<tr><td style="padding:0 0 10px 0;">{cards}</td></tr>'
        f"{note_block}"
        f'<tr><td style="padding:0 0 22px 0;color:{_MUTED};font-size:14px;">'
        f"{_esc(_REPLY[language])}</td></tr>"
        f'<tr><td style="padding:14px 0 0 0;border-top:1px solid {_LINE};font-size:12px;'
        f'color:{_MUTED};">{_esc(_SENT_THROUGH[language])}</td></tr>'
        "</table></td></tr></table></body></html>"
    )


# ── Together ────────────────────────────────────────────────────────────────


def compose(
    *,
    to: Audience,
    language: Language,
    organisation: str,
    order_number: int,
    recipient_name: str,
    due_date: date | None,
    urgency: str,
    documents: Sequence[MailDocument],
    note: str | None,
) -> Composed:
    """Subject, plain-text body and the same words as HTML."""
    letter = _Letter(
        to=to,
        language=language,
        organisation=organisation,
        order_number=order_number,
        recipient_name=recipient_name,
        due_date=due_date,
        urgency=urgency,
        documents=documents,
        note=note.strip() if note and note.strip() else None,
    )
    return Composed(
        subject=_SUBJECT[language].format(number=order_number, organisation=organisation),
        text=_text(letter),
        html=_html(letter),
    )
