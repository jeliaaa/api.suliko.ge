"""The look of every email Suliko sends from its own address.

Password resets, email confirmation, invitations, the personal-account notice.
The order-files email has its own, deliberately plainer layout
(`domain/order_mail_message.py`): that one is a person sending files to a
person, and this one is the platform talking.

## One description, two renderings

A message is a list of blocks (a paragraph, a button, numbered steps, a facts
card, a callout). `compose` turns the same list into the plain-text body and
the HTML body, so the two cannot drift apart and nobody keeps two copies in
sync. The plain text is still the first part of the message: a client that
cannot show HTML, and a spam filter comparing the two, get the same words.

## What the HTML does and does not do

- Tables and inline styles, because mail clients ignore most of modern CSS.
  The `<style>` block only adds what clients that support it can use: narrow
  screens, and dark mode.
- No images. They are blocked by default in most clients, they need hosting,
  and a blocked image leaves a hole. The wordmark is text.
- Everything a person typed (a name, an organisation, an address) is escaped.
  The sentences around it are ours.
- Links are always shown as text as well as behind the button, so a person can
  check where a link goes before opening it, and still use it when the button
  is stripped.
"""

from __future__ import annotations

import html as _html
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, NamedTuple

# Suliko's blue (the same as on suliko.ge and in the order-files email).
_ACCENT = "#3b59f3"
_ACCENT_DARK = "#2339b8"
_ACCENT_LIGHT = "#5b76ff"
_TINT = "#eef1ff"
_TINT_LINE = "#d6dcfb"
_INK = "#111827"
_MUTED = "#6b7280"
_LINE = "#e5e7eb"
_PAGE = "#f3f4f8"
_CARD = "#ffffff"
_WARN_BG = "#fff7ed"
_WARN_LINE = "#fed7aa"
_WARN_INK = "#9a3412"
_FONT = (
    "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Noto Sans Georgian',"
    "'Helvetica Neue',Arial,sans-serif"
)
_MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"

PRODUCT = "Suliko"


class Email(NamedTuple):
    """A finished message: subject, plain-text body, HTML body."""

    subject: str
    text: str
    html: str


# ── Inline pieces ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Bold:
    text: str


@dataclass(frozen=True, slots=True)
class Link:
    """Inline link. In plain text it reads "text (url)"."""

    text: str
    url: str


Part = str | Bold | Link


# ── Blocks ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Paragraph:
    parts: Sequence[Part]


@dataclass(frozen=True, slots=True)
class Fine:
    """Smaller, muted: the "if this was not you" kind of sentence."""

    parts: Sequence[Part]


@dataclass(frozen=True, slots=True)
class Button:
    label: str
    url: str
    #: "primary" is the one thing to do. "secondary" is an outlined button for a
    #: step that comes before it.
    kind: Literal["primary", "secondary"] = "primary"
    #: Show the link under the button, for when the button is stripped.
    show_link: bool = True


@dataclass(frozen=True, slots=True)
class Step:
    parts: Sequence[Part]
    action: Button | None = None


@dataclass(frozen=True, slots=True)
class Steps:
    steps: Sequence[Step]


@dataclass(frozen=True, slots=True)
class Facts:
    """A small card of label and value pairs."""

    rows: Sequence[tuple[str, str]]


@dataclass(frozen=True, slots=True)
class Callout:
    parts: Sequence[Part]
    tone: Literal["info", "warning"] = "info"


Block = Paragraph | Fine | Button | Steps | Facts | Callout


def p(*parts: Part) -> Paragraph:
    return Paragraph(parts)


def fine(*parts: Part) -> Fine:
    return Fine(parts)


def callout(*parts: Part, tone: Literal["info", "warning"] = "info") -> Callout:
    return Callout(parts, tone)


# ── Plain text ──────────────────────────────────────────────────────────────


def _plain(parts: Sequence[Part]) -> str:
    out: list[str] = []
    for part in parts:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, Bold) or part.text in (part.url, part.url.removeprefix("mailto:")):
            out.append(part.text)
        else:
            out.append(f"{part.text} ({part.url})")
    return "".join(out)


def _text_button(button: Button, indent: str = "") -> str:
    return f"{indent}{button.label}:\n{indent}{button.url}"


def _text_block(block: Block) -> str:
    if isinstance(block, (Paragraph, Fine, Callout)):
        return _plain(block.parts)
    if isinstance(block, Button):
        return _text_button(block)
    if isinstance(block, Facts):
        return "\n".join(f"{label}: {value}" for label, value in block.rows)
    lines: list[str] = []
    for number, step in enumerate(block.steps, start=1):
        lines.append(f"{number}. {_plain(step.parts)}")
        if step.action is not None:
            lines.append(_text_button(step.action, indent="   "))
    return "\n".join(lines)


def _text(heading: str, blocks: Sequence[Block], reason: str | None) -> str:
    parts = [heading, *(_text_block(block) for block in blocks)]
    footer = [f"{PRODUCT} Office"]
    if reason:
        footer.insert(0, reason)
    parts.append("--\n" + "\n".join(footer))
    return "\n\n".join(parts) + "\n"


# ── HTML ────────────────────────────────────────────────────────────────────


def _esc(value: str) -> str:
    return _html.escape(value, quote=True)


def _inline(parts: Sequence[Part]) -> str:
    out: list[str] = []
    for part in parts:
        if isinstance(part, str):
            out.append(_esc(part))
        elif isinstance(part, Bold):
            out.append(f"<strong>{_esc(part.text)}</strong>")
        else:
            out.append(
                f'<a class="link" href="{_esc(part.url)}" '
                f'style="color:{_ACCENT};text-decoration:underline;">{_esc(part.text)}</a>'
            )
    return "".join(out)


def _row(inner: str, *, top: int = 0, bottom: int = 16, rule: bool = False) -> str:
    """One row of the body: 32px in from the card's edge (22px on a phone)."""
    border = f"border-top:1px solid {_LINE};" if rule else ""
    klass = "px line" if rule else "px"
    return (
        f'<tr><td class="{klass}" style="padding:{top}px 32px {bottom}px 32px;{border}">'
        f"{inner}</td></tr>"
    )


def _button_html(button: Button) -> str:
    if button.kind == "primary":
        face = (
            f'<td bgcolor="{_ACCENT}" style="background:{_ACCENT};border-radius:10px;">'
            f'<a href="{_esc(button.url)}" style="display:inline-block;padding:14px 30px;'
            f"font-family:{_FONT};font-size:16px;font-weight:700;line-height:20px;"
            f'color:#ffffff;text-decoration:none;border-radius:10px;">{_esc(button.label)}</a></td>'
        )
    else:
        face = (
            f'<td class="btn2" style="border:2px solid {_ACCENT};border-radius:10px;">'
            f'<a class="link" href="{_esc(button.url)}" style="display:inline-block;'
            f"padding:11px 26px;font-family:{_FONT};font-size:15px;font-weight:700;"
            f'line-height:20px;color:{_ACCENT};text-decoration:none;border-radius:10px;">'
            f"{_esc(button.label)}</a></td>"
        )
    link_line = (
        f'<div class="muted" style="padding:10px 0 0 0;font-size:12px;line-height:1.5;'
        f'color:{_MUTED};">Or copy this link into your browser:<br>'
        f'<a class="link" href="{_esc(button.url)}" style="font-family:{_MONO};'
        f'font-size:12px;color:{_ACCENT};word-break:break-all;">{_esc(button.url)}</a></div>'
        if button.show_link
        else ""
    )
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0">'
        f"<tr>{face}</tr></table>{link_line}"
    )


def _html_block(block: Block, *, rule: bool = False) -> str:
    if isinstance(block, Paragraph):
        return _row(
            f'<div class="ink" style="font-size:16px;line-height:1.6;color:{_INK};">'
            f"{_inline(block.parts)}</div>"
        )
    if isinstance(block, Fine):
        return _row(
            f'<div class="muted" style="font-size:13px;line-height:1.6;color:{_MUTED};">'
            f"{_inline(block.parts)}</div>",
            top=18 if rule else 0,
            bottom=10,
            rule=rule,
        )
    if isinstance(block, Button):
        return _row(_button_html(block), top=4, bottom=24)
    if isinstance(block, Callout):
        if block.tone == "warning":
            klass, bg, line, ink = "warn", _WARN_BG, _WARN_LINE, _WARN_INK
        else:
            klass, bg, line, ink = "tint", _TINT, _TINT_LINE, _ACCENT_DARK
        return _row(
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
            f'<tr><td class="{klass}" bgcolor="{bg}" style="background:{bg};'
            f"border:1px solid {line};border-radius:10px;padding:12px 16px;"
            f'font-size:14px;line-height:1.55;color:{ink};">{_inline(block.parts)}</td></tr></table>'
        )
    if isinstance(block, Facts):
        rows = "".join(
            f'<tr><td class="muted" valign="top" style="padding:7px 0;width:38%;font-size:13px;'
            f'color:{_MUTED};">{_esc(label)}</td>'
            f'<td class="ink" valign="top" style="padding:7px 0;font-size:15px;font-weight:600;'
            f'color:{_INK};word-break:break-word;">{_esc(value)}</td></tr>'
            for label, value in block.rows
        )
        return _row(
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
            f'<tr><td class="tint" bgcolor="{_TINT}" style="background:{_TINT};'
            f'border:1px solid {_TINT_LINE};border-radius:10px;padding:8px 16px;">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
            f"{rows}</table></td></tr></table>"
        )
    cards: list[str] = []
    for number, step in enumerate(block.steps, start=1):
        action = (
            f'<div style="padding:12px 0 0 0;">{_button_html(step.action)}</div>'
            if step.action is not None
            else ""
        )
        cards.append(
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            'class="line" style="margin:0 0 10px 0;border:1px solid '
            f'{_LINE};border-radius:12px;border-collapse:separate;"><tr>'
            f'<td valign="top" width="52" style="padding:16px 0 16px 16px;">'
            f'<div style="width:28px;height:28px;border-radius:14px;background:{_ACCENT};'
            f"color:#ffffff;font-family:{_FONT};font-size:14px;font-weight:700;"
            f'line-height:28px;text-align:center;">{number}</div></td>'
            f'<td class="ink" valign="top" style="padding:16px 16px 16px 8px;font-size:15px;'
            f'line-height:1.55;color:{_INK};">{_inline(step.parts)}{action}</td></tr></table>'
        )
    return _row("".join(cards), bottom=8)


_STYLE = """
  body, table, td, a { -webkit-text-size-adjust:100%; -ms-text-size-adjust:100%; }
  table, td { mso-table-lspace:0; mso-table-rspace:0; }
  a { word-break:break-word; }
  @media only screen and (max-width:620px) {
    .container { width:100% !important; }
    .px { padding-left:22px !important; padding-right:22px !important; }
    .hero { padding:26px 22px 28px 22px !important; }
    .h1 { font-size:25px !important; }
  }
  @media (prefers-color-scheme:dark) {
    .bg { background:#0f1220 !important; }
    .card { background:#181b2b !important; border-color:#272b42 !important; }
    .ink { color:#f3f4f6 !important; }
    .muted { color:#9ca3af !important; }
    .line { border-color:#2c3150 !important; }
    .tint {
      background:#212850 !important; border-color:#34407a !important;
      color:#c9d2ff !important;
    }
    .warn {
      background:#3a2616 !important; border-color:#6b3d17 !important;
      color:#fdba74 !important;
    }
    .link { color:#9db0ff !important; }
    .btn2 { border-color:#9db0ff !important; }
  }
""".strip()


def _wrap(heading: str, preheader: str, blocks: Sequence[Block], reason: str | None) -> str:
    rows: list[str] = []
    for index, block in enumerate(blocks):
        # The fine print starts under a hairline, once, where it first appears.
        starts_fine_print = (
            isinstance(block, Fine) and index > 0 and not isinstance(blocks[index - 1], Fine)
        )
        rows.append(_html_block(block, rule=starts_fine_print))
    body = "".join(rows)
    # Padding characters push the rest of the message out of the inbox preview.
    pad = "&#847;&zwnj;&nbsp;" * 40
    reason_html = (
        f'<div class="muted" style="padding:0 0 6px 0;">{_esc(reason)}</div>' if reason else ""
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        '<meta name="supported-color-schemes" content="light dark">'
        f"<title>{_esc(heading)}</title><style>{_STYLE}</style></head>"
        f'<body class="bg" style="margin:0;padding:0;background:{_PAGE};">'
        '<div style="display:none;max-height:0;overflow:hidden;opacity:0;mso-hide:all;'
        f'font-size:1px;line-height:1px;color:{_PAGE};">{_esc(preheader)}{pad}</div>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'class="bg" bgcolor="{_PAGE}" style="background:{_PAGE};">'
        '<tr><td align="center" style="padding:32px 12px 40px 12px;">'
        '<table role="presentation" class="container" width="560" cellpadding="0" cellspacing="0" '
        f'border="0" style="width:560px;max-width:100%;font-family:{_FONT};">'
        # The card: a blue band with the wordmark and the heading, then the body.
        f'<tr><td class="card" bgcolor="{_CARD}" style="background:{_CARD};'
        f'border:1px solid {_LINE};border-radius:18px;overflow:hidden;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        f'<tr><td class="hero" bgcolor="{_ACCENT}" style="background:{_ACCENT};'
        f"background-image:linear-gradient(135deg,{_ACCENT_LIGHT} 0%,{_ACCENT} 45%,"
        f'{_ACCENT_DARK} 100%);padding:30px 32px 34px 32px;">'
        f'<div style="font-size:15px;font-weight:800;letter-spacing:.14em;text-transform:uppercase;'
        f'color:#ffffff;">{PRODUCT}<span style="font-weight:400;opacity:.8;"> Office</span></div>'
        f'<div class="h1" style="padding:18px 0 0 0;font-size:28px;font-weight:800;'
        f'line-height:1.2;color:#ffffff;">{_esc(heading)}</div></td></tr>'
        '<tr><td style="padding:30px 0 18px 0;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        f"{body}</table></td></tr></table></td></tr>"
        '<tr><td class="px muted" style="padding:20px 8px 0 8px;font-size:12px;line-height:1.6;'
        f'color:{_MUTED};text-align:center;">{reason_html}'
        f"<div>{PRODUCT} Office &middot; suliko.ge</div></td></tr>"
        "</table></td></tr></table></body></html>"
    )


# ── Together ────────────────────────────────────────────────────────────────


def compose(
    *,
    subject: str,
    heading: str,
    preheader: str,
    blocks: Sequence[Block],
    reason: str | None = None,
) -> Email:
    """Subject, plain-text body and HTML body from one list of blocks.

    `preheader` is the grey line an inbox shows after the subject. `reason` is
    the footer line that says why this person got the message.
    """
    return Email(
        subject=subject,
        text=_text(heading, blocks, reason),
        html=_wrap(heading, preheader, blocks, reason),
    )


__all__ = [
    "Block",
    "Bold",
    "Button",
    "Callout",
    "Email",
    "Facts",
    "Fine",
    "Link",
    "Paragraph",
    "Step",
    "Steps",
    "callout",
    "compose",
    "fine",
    "p",
]
