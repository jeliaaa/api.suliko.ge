"""A translation's HTML as a Word document (.docx).

## Why this exists

Suliko Translate answers in HTML. What a bureau hands a client is a Word file:
it opens anywhere, a translator finishes it in Word, and a mail preview can
show it. So the translation is filed as a .docx, built here from the HTML.

Not the wrapper suliko.ge's own Word export writes (an empty document that
points at an embedded web page, which only Word can draw): this is a real
document, with paragraphs, headings, lists and tables of its own.

## What it reads

The model is told to answer in a small vocabulary: headings, paragraphs,
`strong`/`em`/`u`, lists, tables, and spans with a colour, a background or an
alignment. The page where a translation is corrected adds what a browser's
editor writes (`div`, `br`, `b`, `i`, `font`). That is what this understands.
Anything else is read as the text it holds; nothing is guessed at.

## What it leaves out, on purpose

- Scripts, style sheets, classes and anything that has to be fetched. The
  HTML is a model's work from a client's document, so it is never run and no
  address in it is ever opened. Only an image carried inside the HTML itself
  (a `data:` address) is kept.
- Links keep their address only when it is http, https, mailto or tel.
- Fonts are not carried over: the document is set in Calibri, like the Word
  files suliko.ge writes, which has the Georgian letters. Sizes, colours,
  alignment, emphasis and table shading are.

A4, 2 cm margins, 11 pt, a little space after each paragraph and none inside
table cells and list items, so a translation takes about the pages the
original did.

A conversion that cannot be done (a document too large or too deeply nested)
raises `DocxConversionError`; the caller keeps the HTML rather than lose a
translation that has already been paid for.
"""

from __future__ import annotations

import base64
import binascii
import io
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from docx import Document
from docx.document import Document as DocumentObject
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.opc.constants import RELATIONSHIP_TYPE
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from docx.table import _Cell
from docx.text.paragraph import Paragraph
from docx.text.run import Run
from lxml import etree
from lxml import html as lxml_html

DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

FONT = "Calibri"
MONOSPACE = "Consolas"
BASE_SIZE = 11.0
PARAGRAPH_AFTER = Pt(4)

#: Beyond these a document is not one a person made: refuse rather than spend
#: a minute building it.
MAX_NODES = 200_000
MAX_DEPTH = 60
MAX_TABLE_CELLS = 20_000
MAX_COLUMNS = 63  # Word's own limit
MAX_IMAGE_BYTES = 5 * 1024 * 1024
TEXT_WIDTH = Cm(17)  # A4 less two 2 cm margins

_COLLAPSIBLE = re.compile(r"[ \t\n\r\f]+")
#: Characters XML cannot carry: python-docx refuses a string that has one.
_XML_FORBIDDEN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_XML_PROLOG = re.compile(r"^\s*<\?xml[^>]*\?>", re.IGNORECASE)
_LENGTH = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*(px|pt|cm|mm|in|em|rem|%)?\s*$", re.IGNORECASE)
_RGB_FUNCTION = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", re.IGNORECASE)
_HEX_COLOR = re.compile(r"^#([0-9a-f]{3}|[0-9a-f]{6})$", re.IGNORECASE)

_NAMED_COLORS = {
    "black": "000000",
    "white": "FFFFFF",
    "red": "FF0000",
    "green": "008000",
    "blue": "0000FF",
    "yellow": "FFFF00",
    "orange": "FFA500",
    "purple": "800080",
    "gray": "808080",
    "grey": "808080",
    "silver": "C0C0C0",
    "maroon": "800000",
    "navy": "000080",
    "teal": "008080",
    "olive": "808000",
    "lime": "00FF00",
    "aqua": "00FFFF",
    "cyan": "00FFFF",
    "fuchsia": "FF00FF",
    "magenta": "FF00FF",
    "brown": "A52A2A",
    "pink": "FFC0CB",
}

_FONT_KEYWORDS = {
    "xx-small": 7.0,
    "x-small": 8.0,
    "small": 9.5,
    "medium": BASE_SIZE,
    "large": 14.0,
    "x-large": 18.0,
    "xx-large": 24.0,
}
#: What `<font size="1..7">`, which a browser's editor writes, stands for.
_FONT_TAG_SIZES = {1: 8.0, 2: 10.0, 3: BASE_SIZE, 4: 14.0, 5: 18.0, 6: 24.0, 7: 36.0}

_HEADING_SIZES = {1: 14.0, 2: 13.0, 3: 12.0, 4: 11.0, 5: 11.0, 6: 11.0}

#: Not content: skipped with everything inside.
_SKIPPED = frozenset(
    {
        "head",
        "script",
        "style",
        "title",
        "meta",
        "link",
        "noscript",
        "template",
        "iframe",
        "object",
        "embed",
        "svg",
        "canvas",
        "audio",
        "video",
        "button",
        "select",
        "textarea",
        "input",
        "map",
    }
)
#: Containers that are a block of their own but add nothing else.
_SECTIONS = frozenset(
    {
        "html",
        "body",
        "div",
        "section",
        "article",
        "header",
        "footer",
        "main",
        "aside",
        "nav",
        "figure",
        "figcaption",
        "address",
        "form",
        "fieldset",
        "center",
        "details",
        "summary",
        "dl",
        "dt",
        "dd",
    }
)
_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_BLOCKS = _SECTIONS | _HEADINGS | {"p", "blockquote", "pre", "caption"}
_TABLE_PARTS = frozenset({"table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption"})
_SAFE_LINK_SCHEMES = frozenset({"http", "https", "mailto", "tel"})

_ALIGNMENTS = {
    "left": WD_ALIGN_PARAGRAPH.LEFT,
    "start": WD_ALIGN_PARAGRAPH.LEFT,
    "center": WD_ALIGN_PARAGRAPH.CENTER,
    "right": WD_ALIGN_PARAGRAPH.RIGHT,
    "end": WD_ALIGN_PARAGRAPH.RIGHT,
    "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
}

_BULLETS = ("•", "◦", "▪")
_NUMBER_FORMATS = ("decimal", "lowerLetter", "lowerRoman")
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


class DocxConversionError(Exception):
    """The HTML could not be made into a document."""


# ── Reading CSS and attributes ──────────────────────────────────────────────


def _styles(element: Any) -> dict[str, str]:
    """The declarations of an element's `style` attribute, names in lower case."""
    out: dict[str, str] = {}
    for declaration in (element.get("style") or "").split(";"):
        name, colon, value = declaration.partition(":")
        if colon:
            out[name.strip().lower()] = value.replace("!important", "").strip()
    return out


def _color(value: str | None) -> str | None:
    """A colour as six hex digits, or None for one that is not a colour to apply."""
    if not value:
        return None
    text = value.strip().lower()
    if match := _HEX_COLOR.match(text):
        digits = match.group(1)
        if len(digits) == 3:
            digits = "".join(c * 2 for c in digits)
        return digits.upper()
    if match := _RGB_FUNCTION.match(text):
        parts = [min(255, int(part)) for part in match.groups()]
        return "".join(f"{part:02X}" for part in parts)
    return _NAMED_COLORS.get(text)


def _points(value: str | None, *, base: float = BASE_SIZE) -> float | None:
    """A CSS length in points. Percentages and `em` are of `base`."""
    if not value:
        return None
    match = _LENGTH.match(value)
    if match is None:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "px").lower()
    factor = {
        "px": 0.75,
        "pt": 1.0,
        "cm": 72 / 2.54,
        "mm": 72 / 25.4,
        "in": 72.0,
        "em": base,
        "rem": BASE_SIZE,
        "%": base / 100,
    }[unit]
    return number * factor


def _font_size(value: str | None) -> float | None:
    if not value:
        return None
    keyword = value.strip().lower()
    if keyword in _FONT_KEYWORDS:
        return _FONT_KEYWORDS[keyword]
    size = _points(value)
    return None if size is None else min(72.0, max(6.0, size))


def _truthy_weight(value: str) -> bool:
    text = value.strip().lower()
    if text in {"bold", "bolder"}:
        return True
    return text.isdigit() and int(text) >= 600


# ── What applies to the text being written ──────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Format:
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    superscript: bool = False
    subscript: bool = False
    monospace: bool = False
    color: str | None = None
    background: str | None = None
    size: float | None = None
    link: str | None = None


@dataclass(frozen=True, slots=True)
class _Context:
    """What an element passes down to what is inside it."""

    format: _Format = _Format()
    #: A built-in paragraph style ("Heading 2"); None for the body's own.
    style: str | None = None
    align: WD_ALIGN_PARAGRAPH | None = None
    indent: float = 0.0
    #: No space after a paragraph: inside a table cell or a list item.
    tight: bool = False
    preformatted: bool = False
    depth: int = 0
    list_level: int = -1
    #: (numbering id) of the list being written, and whether it counts.
    list_num_id: int | None = None


def _apply_element(fmt: _Format, element: Any, tag: str) -> _Format:
    """The format of an element's text: its tag, then its own `style`."""
    if tag in {"b", "strong", "th"}:
        fmt = replace(fmt, bold=True)
    elif tag in {"i", "em", "cite", "dfn", "var"}:
        fmt = replace(fmt, italic=True)
    elif tag in {"u", "ins"}:
        fmt = replace(fmt, underline=True)
    elif tag in {"s", "strike", "del"}:
        fmt = replace(fmt, strike=True)
    elif tag == "sup":
        fmt = replace(fmt, superscript=True, subscript=False)
    elif tag == "sub":
        fmt = replace(fmt, subscript=True, superscript=False)
    elif tag in {"code", "kbd", "samp", "tt", "pre"}:
        fmt = replace(fmt, monospace=True)
    elif tag == "mark":
        fmt = replace(fmt, background="FFFF00")
    elif tag == "font":
        fmt = replace(fmt, color=_color(element.get("color")) or fmt.color)
        raw = (element.get("size") or "").strip()
        if raw.isdigit() and int(raw) in _FONT_TAG_SIZES:
            tag_size = _FONT_TAG_SIZES[int(raw)]
            fmt = replace(fmt, size=None if tag_size == BASE_SIZE else tag_size)
    elif tag == "a":
        href = (element.get("href") or "").strip()
        if urlparse(href).scheme.lower() in _SAFE_LINK_SCHEMES:
            fmt = replace(fmt, link=href)

    css = _styles(element)
    if "color" in css and (color := _color(css["color"])):
        fmt = replace(fmt, color=color)
    background = _color(css.get("background-color") or css.get("background"))
    if background:
        fmt = replace(fmt, background=background)
    if "font-size" in css and (size := _font_size(css["font-size"])):
        fmt = replace(fmt, size=None if size == BASE_SIZE else size)
    if "font-weight" in css:
        fmt = replace(fmt, bold=_truthy_weight(css["font-weight"]))
    if css.get("font-style", "").lower() in {"italic", "oblique"}:
        fmt = replace(fmt, italic=True)
    decoration = css.get("text-decoration", css.get("text-decoration-line", "")).lower()
    if "underline" in decoration:
        fmt = replace(fmt, underline=True)
    if "line-through" in decoration:
        fmt = replace(fmt, strike=True)
    vertical = css.get("vertical-align", "").lower()
    if vertical == "super":
        fmt = replace(fmt, superscript=True, subscript=False)
    elif vertical == "sub":
        fmt = replace(fmt, subscript=True, superscript=False)
    family = css.get("font-family", "").lower()
    if any(name in family for name in ("monospace", "courier", "consolas")):
        fmt = replace(fmt, monospace=True)
    return fmt


def _element_format(parent: _Format, element: Any, tag: str) -> _Format:
    """`_apply_element`, except that a table's shading is the cells', not the text's."""
    fmt = _apply_element(parent, element, tag)
    return replace(fmt, background=parent.background) if tag in _TABLE_PARTS else fmt


def _alignment(element: Any) -> WD_ALIGN_PARAGRAPH | None:
    value = (_styles(element).get("text-align") or element.get("align") or "").strip().lower()
    return _ALIGNMENTS.get(value)


def _left_indent(element: Any) -> float:
    css = _styles(element)
    for name in ("margin-left", "padding-left"):
        points = _points(css.get(name))
        if points:
            return min(300.0, max(0.0, points))
    return 0.0


def _page_break(element: Any, which: str) -> bool:
    css = _styles(element)
    old = css.get(f"page-break-{which}", "").lower()
    new = css.get(f"break-{which}", "").lower()
    return old == "always" or new in {"page", "always"}


# ── Where text goes ─────────────────────────────────────────────────────────


class _Cursor:
    """The paragraph being written to, in one container (the page, or a cell).

    Paragraphs open when text arrives, not when a block starts, so a `div`
    that holds a few words and then a table gives a paragraph, the table, and
    nothing empty between them. Whitespace is collapsed the way a browser does
    it: the space at the start and end of a line goes, and a break at the end
    of a block does not make a blank line.
    """

    def __init__(self, container: DocumentObject | _Cell, spare: Paragraph | None = None) -> None:
        self.container = container
        #: The empty paragraph a table cell starts with, used before adding one.
        self.spare = spare
        self.paragraph: Paragraph | None = None
        self.line_has_text = False
        self.pending_space = False
        #: Line breaks seen but not written yet: one at the end of a block
        #: makes no blank line, so they are written only when text follows.
        self.pending_breaks = 0
        #: Numbering for the next paragraph opened, set by a list item.
        self.numbering: tuple[int, int] | None = None
        self.after_table = False
        #: The last paragraph opened, to give a list its space after it.
        self.last: Paragraph | None = None

    def close(self) -> None:
        self.paragraph = None
        self.line_has_text = False
        self.pending_space = False
        self.pending_breaks = 0


class _Writer:
    def __init__(self, language: str | None) -> None:
        self.document: DocumentObject = Document()
        self.nodes = 0
        self.page_break_next = False
        self._bullet_num: int | None = None
        self._abstract: dict[bool, int] = {}
        self._prepare(language)

    # ── The empty document ────────────────────────────────────────────────

    def _prepare(self, language: str | None) -> None:
        section = self.document.sections[0]
        section.page_width, section.page_height = Cm(21), Cm(29.7)
        section.left_margin = section.right_margin = Cm(2)
        section.top_margin = section.bottom_margin = Cm(2)

        styles = self.document.styles
        normal = styles["Normal"]
        self._set_font(normal, FONT, BASE_SIZE)
        normal.paragraph_format.space_before = Pt(0)
        normal.paragraph_format.space_after = PARAGRAPH_AFTER
        normal.paragraph_format.line_spacing = 1.0
        if language and re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?", language):
            # Last in the style's run properties, where the schema wants it:
            # it is what makes Word check spelling in the translation's language.
            lang = OxmlElement("w:lang")
            lang.set(qn("w:val"), language)
            normal.element.get_or_add_rPr().append(lang)

        for level, size in _HEADING_SIZES.items():
            heading = styles[f"Heading {level}"]
            self._set_font(heading, FONT, size)
            heading.font.bold = True
            heading.font.italic = False
            heading.font.color.rgb = RGBColor(0, 0, 0)
            heading.paragraph_format.space_before = Pt(6)
            heading.paragraph_format.space_after = Pt(3)
            heading.paragraph_format.keep_with_next = True

        properties = self.document.core_properties
        now = datetime.now(UTC)
        properties.author = "Suliko Translate"
        properties.last_modified_by = "Suliko Office"
        properties.comments = ""
        properties.created = now
        properties.modified = now

    @staticmethod
    def _set_font(style: Any, name: str, size: float) -> None:
        """The style's font, by name only: a theme font would win over a name."""
        fonts = style.element.get_or_add_rPr().get_or_add_rFonts()
        for attribute in list(fonts.attrib):
            del fonts.attrib[attribute]
        for attribute in ("ascii", "hAnsi", "eastAsia", "cs"):
            fonts.set(qn(f"w:{attribute}"), name)
        style.font.size = Pt(size)

    # ── Lists ─────────────────────────────────────────────────────────────

    def _abstract_num(self, *, bullet: bool) -> int:
        """A multilevel list definition, added once: bullets, or numbers that
        go 1. a. i. and round again."""
        if bullet in self._abstract:
            return self._abstract[bullet]
        numbering = self.document.part.numbering_part.element
        used = [
            int(node.get(qn("w:abstractNumId"))) for node in numbering.findall(qn("w:abstractNum"))
        ]
        identifier = max(used, default=-1) + 1
        levels = []
        for level in range(9):
            if bullet:
                number_format, text = "bullet", _BULLETS[level % 3]
            else:
                number_format, text = _NUMBER_FORMATS[level % 3], f"%{level + 1}."
            levels.append(
                f'<w:lvl w:ilvl="{level}"><w:start w:val="1"/>'
                f'<w:numFmt w:val="{number_format}"/><w:lvlText w:val="{text}"/>'
                f'<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="{567 * (level + 1)}" '
                'w:hanging="340"/></w:pPr><w:rPr><w:rFonts w:ascii="Calibri" '
                'w:hAnsi="Calibri" w:cs="Calibri" w:hint="default"/></w:rPr></w:lvl>'
            )
        node = parse_xml(
            f'<w:abstractNum xmlns:w="{_W}" w:abstractNumId="{identifier}">'
            f'<w:multiLevelType w:val="hybridMultilevel"/>{"".join(levels)}</w:abstractNum>'
        )
        # Every abstract definition has to come before the first numbering.
        first = numbering.find(qn("w:num"))
        if first is not None:
            first.addprevious(node)
        else:
            numbering.append(node)
        self._abstract[bullet] = identifier
        return identifier

    def _list_numbering(self, *, ordered: bool, start: int) -> int:
        """The numbering a new list uses. A numbered list gets its own, so it
        starts again at 1 (or at its `start`); bullets share one."""
        numbering = self.document.part.numbering_part.element
        if not ordered and self._bullet_num is not None:
            return self._bullet_num
        num = numbering.add_num(self._abstract_num(bullet=not ordered))
        if ordered:
            for level in range(9):
                override = num.add_lvlOverride(ilvl=level)
                override.add_startOverride(start if level == 0 else 1)
        else:
            self._bullet_num = num.numId
        return int(num.numId)

    # ── Paragraphs and runs ───────────────────────────────────────────────

    def _open(self, cursor: _Cursor, ctx: _Context) -> Paragraph:
        """The paragraph text goes into, opened if there is none."""
        if cursor.paragraph is not None:
            return cursor.paragraph
        if cursor.spare is not None:
            paragraph, cursor.spare = cursor.spare, None
        else:
            paragraph = cursor.container.add_paragraph()
        if ctx.style:
            paragraph.style = self.document.styles[ctx.style]
        layout = paragraph.paragraph_format
        if ctx.align is not None:
            paragraph.alignment = ctx.align
        if ctx.indent:
            layout.left_indent = Pt(ctx.indent)
        if ctx.tight:
            layout.space_after = Pt(0)
        if cursor.after_table:
            layout.space_before = Pt(6)
            cursor.after_table = False
        if self.page_break_next:
            layout.page_break_before = True
            self.page_break_next = False
        if cursor.numbering is not None:
            num_id, level = cursor.numbering
            cursor.numbering = None
            numbering = paragraph._p.get_or_add_pPr().get_or_add_numPr()
            numbering.get_or_add_ilvl().val = level
            numbering.get_or_add_numId().val = num_id
        cursor.paragraph = paragraph
        cursor.last = paragraph
        cursor.line_has_text = False
        return paragraph

    def _write(self, cursor: _Cursor, ctx: _Context, text: str) -> None:
        paragraph = self._open(cursor, ctx)
        fmt = ctx.format
        if fmt.link:
            part = paragraph.part
            relation = part.relate_to(fmt.link, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
            link = OxmlElement("w:hyperlink")
            link.set(qn("r:id"), relation)
            run = Run(OxmlElement("w:r"), paragraph)
            link.append(run._r)
            paragraph._p.append(link)
        else:
            run = paragraph.add_run()
        self._style_run(run, fmt)
        # The run's text setter turns "\n" into a line break and "\t" into a tab.
        run.text = "\n" * cursor.pending_breaks + text
        cursor.pending_breaks = 0
        cursor.line_has_text = True

    @staticmethod
    def _style_run(run: Run, fmt: _Format) -> None:
        if fmt.background:
            # First, so the properties python-docx adds are put before it.
            shading = OxmlElement("w:shd")
            shading.set(qn("w:val"), "clear")
            shading.set(qn("w:color"), "auto")
            shading.set(qn("w:fill"), fmt.background)
            run._r.get_or_add_rPr().append(shading)
        font = run.font
        if fmt.bold:
            font.bold = True
        if fmt.italic:
            font.italic = True
        if fmt.underline or (fmt.link and not fmt.color):
            font.underline = True
        if fmt.strike:
            font.strike = True
        if fmt.superscript:
            font.superscript = True
        if fmt.subscript:
            font.subscript = True
        if fmt.monospace:
            font.name = MONOSPACE
        if fmt.size:
            font.size = Pt(min(72.0, max(6.0, fmt.size)))
        if fmt.color:
            font.color.rgb = RGBColor.from_string(fmt.color)
        elif fmt.link:
            font.color.rgb = RGBColor.from_string("0563C1")

    def _text(self, cursor: _Cursor, ctx: _Context, raw: str | None) -> None:
        if not raw:
            return
        text = _XML_FORBIDDEN.sub("", raw)
        if ctx.preformatted:
            if text.strip("\n"):
                self._write(cursor, ctx, text.strip("\n"))
            return
        collapsed = _COLLAPSIBLE.sub(" ", text)
        core = collapsed.strip(" ")
        if not core:
            if collapsed and cursor.line_has_text:
                cursor.pending_space = True
            return
        fmt = ctx.format
        lead = (collapsed.startswith(" ") or cursor.pending_space) and cursor.line_has_text
        if lead and (fmt.underline or fmt.strike or fmt.background or fmt.link):
            # The space between "a" and an underlined word is not underlined.
            plain = replace(fmt, underline=False, strike=False, background=None, link=None)
            self._write(cursor, replace(ctx, format=plain), " ")
            lead = False
        self._write(cursor, ctx, (" " if lead else "") + core)
        cursor.pending_space = collapsed.endswith(" ")

    def _line_break(self, cursor: _Cursor, ctx: _Context) -> None:
        self._open(cursor, ctx)
        cursor.pending_breaks += 1
        cursor.line_has_text = False
        cursor.pending_space = False

    def _image(self, cursor: _Cursor, ctx: _Context, element: Any) -> None:
        """An image carried inside the HTML; one that has to be fetched is not."""
        source = (element.get("src") or "").strip()
        match = re.match(
            r"^data:image/[a-z0-9.+-]+;base64,(.+)$", source, re.IGNORECASE | re.DOTALL
        )
        if match is None:
            return
        try:
            data = base64.b64decode(match.group(1), validate=False)
        except (binascii.Error, ValueError):
            return
        if not data or len(data) > MAX_IMAGE_BYTES:
            return
        paragraph = self._open(cursor, ctx)
        try:
            width = _points(element.get("width"))
            run = paragraph.add_run()
            run.add_picture(io.BytesIO(data), width=Pt(min(width, 480)) if width else None)
        except Exception:
            # An image Word cannot read is left out; the translation is not.
            return
        cursor.line_has_text = True

    # ── Walking the HTML ──────────────────────────────────────────────────

    def children(self, cursor: _Cursor, element: Any, ctx: _Context) -> None:
        self._text(cursor, ctx, element.text)
        for child in element:
            self.node(cursor, child, ctx)
            self._text(cursor, ctx, child.tail)

    def node(self, cursor: _Cursor, element: Any, parent: _Context) -> None:
        if not isinstance(element.tag, str):
            return  # a comment or a processing instruction
        self.nodes += 1
        if self.nodes > MAX_NODES or parent.depth > MAX_DEPTH:
            raise DocxConversionError("The document is too large or too deeply nested.")
        tag = element.tag.lower()
        if tag in _SKIPPED:
            return
        ctx = replace(
            parent, depth=parent.depth + 1, format=_element_format(parent.format, element, tag)
        )

        if tag == "br":
            self._line_break(cursor, ctx)
        elif tag == "img":
            self._image(cursor, ctx, element)
        elif tag == "hr":
            self._rule(cursor, ctx)
        elif tag == "table":
            self._table(cursor, element, ctx)
        elif tag in {"ul", "ol"}:
            self._list(cursor, element, ctx, ordered=tag == "ol")
        elif tag == "li":
            self._list_item(cursor, element, ctx)
        else:
            self._block_or_inline(cursor, element, ctx, tag)

    def _block_or_inline(self, cursor: _Cursor, element: Any, ctx: _Context, tag: str) -> None:
        if tag in _BLOCKS:
            self._block(cursor, element, ctx, tag)
        else:
            self.children(cursor, element, ctx)

    def _block(self, cursor: _Cursor, element: Any, ctx: _Context, tag: str) -> None:
        cursor.close()
        if _page_break(element, "before"):
            self.page_break_next = True
        align = _alignment(element)
        inner = replace(
            ctx,
            align=align if align is not None else ctx.align,
            indent=ctx.indent + _left_indent(element),
        )
        if tag in _HEADINGS:
            inner = replace(inner, style=f"Heading {tag[1]}")
        elif tag in {"blockquote", "dd"}:
            inner = replace(inner, indent=inner.indent + 36)
        elif tag == "dt":
            inner = replace(inner, format=replace(inner.format, bold=True))
        elif tag == "caption":
            inner = replace(
                inner, align=WD_ALIGN_PARAGRAPH.CENTER, format=replace(inner.format, bold=True)
            )
        elif tag == "pre":
            inner = replace(inner, preformatted=True)

        self.children(cursor, element, inner)
        cursor.close()
        if _page_break(element, "after"):
            self.page_break_next = True

    def _rule(self, cursor: _Cursor, ctx: _Context) -> None:
        cursor.close()
        paragraph = cursor.spare if cursor.spare is not None else cursor.container.add_paragraph()
        cursor.spare = None
        # The border first: the spacing python-docx adds goes after it.
        border = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        for name, value in (("val", "single"), ("sz", "6"), ("space", "1"), ("color", "808080")):
            bottom.set(qn(f"w:{name}"), value)
        border.append(bottom)
        paragraph._p.get_or_add_pPr().append(border)
        paragraph.paragraph_format.space_after = Pt(6)

    # ── Lists ─────────────────────────────────────────────────────────────

    def _list(self, cursor: _Cursor, element: Any, ctx: _Context, *, ordered: bool) -> None:
        cursor.close()
        level = ctx.list_level + 1
        start = 1
        raw_start = (element.get("start") or "").strip()
        if ordered and raw_start.isdigit():
            start = max(1, int(raw_start))
        inner = replace(
            ctx,
            list_level=min(level, 8),
            list_num_id=self._list_numbering(ordered=ordered, start=start),
            tight=True,
        )
        before = cursor.last
        for child in element:
            self.node(cursor, child, inner)
        cursor.close()
        if level == 0 and cursor.last is not None and cursor.last is not before:
            cursor.last.paragraph_format.space_after = PARAGRAPH_AFTER

    def _list_item(self, cursor: _Cursor, element: Any, ctx: _Context) -> None:
        if ctx.list_num_id is None:
            # An item outside any list: a bullet of its own.
            ctx = replace(
                ctx, list_level=0, list_num_id=self._list_numbering(ordered=False, start=1)
            )
        cursor.close()
        cursor.numbering = (ctx.list_num_id or 0, max(0, ctx.list_level))
        self.children(cursor, element, replace(ctx, tight=True))
        cursor.numbering = None
        cursor.close()

    # ── Tables ────────────────────────────────────────────────────────────

    def _table(self, cursor: _Cursor, element: Any, ctx: _Context) -> None:
        rows = element.xpath("./tr | ./thead/tr | ./tbody/tr | ./tfoot/tr")
        if not rows:
            self.children(cursor, element, ctx)
            return
        cursor.close()
        if _page_break(element, "before"):
            self.page_break_next = True

        placements: list[tuple[int, int, int, int, Any]] = []
        occupied: set[tuple[int, int]] = set()
        for row_index, row in enumerate(rows):
            column = 0
            for cell in row.xpath("./td | ./th"):
                while (row_index, column) in occupied:
                    column += 1
                columns = self._span(cell.get("colspan"), 1, MAX_COLUMNS)
                span_rows = self._span(cell.get("rowspan"), 1, len(rows) - row_index)
                # A span stops where another cell already is: two merges
                # cannot share a square.
                columns = next(
                    (i for i in range(1, columns) if (row_index, column + i) in occupied), columns
                )
                span_rows = next(
                    (
                        i
                        for i in range(1, span_rows)
                        if any((row_index + i, column + c) in occupied for c in range(columns))
                    ),
                    span_rows,
                )
                for r in range(span_rows):
                    for c in range(columns):
                        occupied.add((row_index + r, column + c))
                placements.append((row_index, column, span_rows, columns, cell))
                column += columns
        width = max((column + columns for _, column, _, columns, _ in placements), default=0)
        if width == 0:
            return
        if width > MAX_COLUMNS or len(rows) * width > MAX_TABLE_CELLS:
            raise DocxConversionError("A table is too large.")

        for caption in element.xpath("./caption"):
            self.node(cursor, caption, ctx)
        table = cursor.container.add_table(len(rows), width)
        if self._has_borders(element):
            table.style = self.document.styles["Table Grid"]
        for row_index, column, span_rows, columns, cell in placements:
            target = table.cell(row_index, column)
            if span_rows > 1 or columns > 1:
                target = target.merge(table.cell(row_index + span_rows - 1, column + columns - 1))
            self._cell(target, cell, rows[row_index], ctx)
        cursor.after_table = True

    @staticmethod
    def _span(raw: str | None, minimum: int, maximum: int) -> int:
        try:
            value = int((raw or "").strip() or minimum)
        except ValueError:
            return minimum
        return max(minimum, min(value, max(minimum, maximum)))

    @staticmethod
    def _has_borders(table: Any) -> bool:
        """Whether the table is drawn with lines. HTML's own default is none,
        and a table used to lay out a letterhead has none."""
        border = (table.get("border") or "").strip()
        if border and border != "0":
            return True
        for element in table.iter("table", "tr", "td", "th"):
            css = _styles(element)
            for name, value in css.items():
                if name.startswith("border") and not name.endswith(
                    ("radius", "spacing", "collapse")
                ):
                    lowered = value.lower()
                    if (
                        lowered
                        and not lowered.startswith(("none", "0"))
                        and "hidden" not in lowered
                    ):
                        return True
        return False

    def _cell(self, target: _Cell, cell: Any, row: Any, ctx: _Context) -> None:
        shade = None
        for source in (row, cell):  # the cell's own shading wins over its row's
            css = _styles(source)
            shade = (
                _color(css.get("background-color") or css.get("background"))
                or _color(source.get("bgcolor"))
                or shade
            )
        if shade:
            shading = OxmlElement("w:shd")
            shading.set(qn("w:val"), "clear")
            shading.set(qn("w:color"), "auto")
            shading.set(qn("w:fill"), shade)
            target._tc.get_or_add_tcPr().append(shading)

        align = _alignment(cell)
        if align is None and cell.tag.lower() == "th":
            align = WD_ALIGN_PARAGRAPH.CENTER
        inner = replace(
            ctx,
            format=_element_format(ctx.format, cell, cell.tag.lower()),
            align=align,
            tight=True,
            indent=0.0,
            list_level=-1,
            list_num_id=None,
            style=None,
        )
        cursor = _Cursor(target, spare=target.paragraphs[0])
        self.children(cursor, cell, inner)
        cursor.close()


def html_to_docx(html: str, *, language: str | None = None) -> bytes:
    """The translation as the bytes of a .docx.

    `language` is the code the translation is in ("en", "ka"), so Word checks
    its spelling in that language. Raises `DocxConversionError`.
    """
    text = _XML_PROLOG.sub("", html.lstrip("﻿"))
    try:
        writer = _Writer(language)
        if text.strip():
            try:
                root = lxml_html.document_fromstring(_XML_FORBIDDEN.sub("", text))
            except etree.ParserError:
                root = None  # nothing but comments: an empty document
            if root is not None:
                body = root.find("body")
                writer.children(
                    _Cursor(writer.document), body if body is not None else root, _Context()
                )
                title = root.findtext("head/title")
                if title and title.strip():
                    writer.document.core_properties.title = title.strip()[:255]
        out = io.BytesIO()
        writer.document.save(out)
    except DocxConversionError:
        raise
    except (RecursionError, MemoryError) as exc:
        raise DocxConversionError("The document is too large or too deeply nested.") from exc
    except Exception as exc:
        raise DocxConversionError(f"The HTML could not be converted: {exc}") from exc
    return out.getvalue()


__all__ = ["DOCX_CONTENT_TYPE", "DocxConversionError", "html_to_docx"]
