"""Checking that bytes sent as a Word document are one.

The editor Office embeds writes the .docx in the browser and sends it back, so
the bytes come from a client. They are stored and later emailed or downloaded as
a Word file, so they must be one: a .docx is a zip of XML parts, and this checks
the shape without opening any of it as a document. What it will not take is a
file with macros, and a zip built to be larger than it looks.
"""

from __future__ import annotations

import io
import zipfile

from suliko.core.errors import ValidationError
from suliko.domain.translation_docx import DOCX_CONTENT_TYPE

#: A translation of any length is a few hundred parts at most.
MAX_PARTS = 2_000
#: What the parts add up to once unpacked. Far more than a document of the
#: size Office accepts has; a zip made to swell is refused.
MAX_UNPACKED_BYTES = 300 * 1024 * 1024
_REQUIRED = ("[Content_Types].xml", "word/document.xml")


class InvalidWordDocumentError(ValidationError):
    """The bytes are not a Word document Office will keep."""

    error_code = "invalid_word_document"


def ensure_word_document(content: bytes) -> None:
    """Raise `InvalidWordDocumentError` unless `content` is a .docx without macros."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise InvalidWordDocumentError("This is not a Word document.") from exc

    with archive:
        parts = archive.infolist()
        names = {part.filename for part in parts}
        if len(parts) > MAX_PARTS or sum(part.file_size for part in parts) > MAX_UNPACKED_BYTES:
            raise InvalidWordDocumentError("This Word document is too large to keep.")
        if not all(required in names for required in _REQUIRED):
            raise InvalidWordDocumentError("This is not a Word document.")
        if any(name.lower().startswith("word/vbaproject") for name in names):
            raise InvalidWordDocumentError("Word documents with macros are not kept.")


__all__ = ["DOCX_CONTENT_TYPE", "InvalidWordDocumentError", "ensure_word_document"]
