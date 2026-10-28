"""The Word editor Office embeds, and what only suliko.ge's backend can do for it.

The editor runs in the browser and writes a .docx there, but it cannot read one:
a Word file has to be turned into the editor's own format (SFDT) by a server
with Syncfusion's library, and that is suliko.ge's backend (Api24), which has
the licence and already runs it for the site. Office asks it with the shared
key (``SULIKO_API_KEY``, sent as ``X-Office-Key``), as it does for translations:
``POST /api/office/document-editor/import`` takes a .docx or an .html file and
answers with the editor's JSON.

As in ``suliko_translate.py``, a suliko.ge that is down or misconfigured is
`SulikoUnavailableError`, never a verdict on the file. A file the library
cannot read is `EditorDocumentUnreadableError`.
"""

from __future__ import annotations

from typing import Protocol

import httpx
import structlog

from suliko.config import get_settings
from suliko.core.errors import ValidationError
from suliko.core.slow_requests import upstream_hooks
from suliko.integrations.suliko_backend import KEY_HEADER, SulikoUnavailableError

log = structlog.get_logger()

IMPORT_PATH = "/api/office/document-editor/import"


class EditorDocumentUnreadableError(ValidationError):
    """The editor's server could not open this document."""

    error_code = "editor_document_unreadable"


class WordEditorImporter(Protocol):
    """What the editor routes need. The fake in the tests implements this."""

    async def to_sfdt(self, *, file_name: str, content: bytes) -> str:
        """The document as the editor's JSON text. `file_name` says its type:
        only the extension (.docx or .html) is read."""
        ...

    async def aclose(self) -> None: ...


class HttpWordEditorImporter:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._http = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout, connect=5.0),
            event_hooks=upstream_hooks("word_editor"),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def to_sfdt(self, *, file_name: str, content: bytes) -> str:
        if not self._api_key:
            raise SulikoUnavailableError("Office is not set up to open documents in the editor.")
        try:
            response = await self._http.post(
                IMPORT_PATH,
                headers={KEY_HEADER: self._api_key},
                files={"file": (file_name, content, "application/octet-stream")},
            )
        except httpx.HTTPError as exc:
            log.error("word_editor_unreachable", error=type(exc).__name__)
            raise SulikoUnavailableError("The editor cannot be reached right now.") from exc

        if response.status_code in (400, 413, 422):
            log.warning(
                "word_editor_refused", status=response.status_code, body=response.text[:200]
            )
            raise EditorDocumentUnreadableError(
                "This document could not be opened in the editor. Download it to open it in Word."
            )
        if response.status_code != 200 or not response.text.strip():
            log.error("word_editor_status", status=response.status_code)
            raise SulikoUnavailableError("The editor cannot be reached right now.")
        return response.text


class UnconfiguredWordEditorImporter:
    """No `SULIKO_API_URL`: every call says the editor's server is not there."""

    async def aclose(self) -> None:
        return None

    async def to_sfdt(self, *, file_name: str, content: bytes) -> str:
        raise SulikoUnavailableError("The editor is not set up on this server.")


_importer: WordEditorImporter | None = None


def get_word_importer() -> WordEditorImporter:
    """FastAPI dependency. Built once per process; tests pass their own."""
    global _importer
    if _importer is None:
        settings = get_settings()
        if settings.suliko_backend_enabled and settings.suliko_api_url:
            _importer = HttpWordEditorImporter(
                settings.suliko_api_url,
                settings.suliko_api_key.get_secret_value(),
                settings.word_editor_timeout_seconds,
            )
        else:
            _importer = UnconfiguredWordEditorImporter()
    return _importer


async def close_word_importer() -> None:
    global _importer
    if _importer is not None:
        await _importer.aclose()
    _importer = None
