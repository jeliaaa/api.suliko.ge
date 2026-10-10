"""suliko.ge's translator, asked to work for a person Office names.

Suliko Office has no token of the person's to present to suliko.ge. It holds
the shared key (``SULIKO_API_KEY``, sent as ``X-Office-Key``) and the person's
suliko.ge id, learned when they signed in. suliko.ge takes the pages from THAT
person's balance, by the same rules as a translation started on its own site.

The flow is the site's:

- ``prepare`` hands the file over. suliko.ge measures it (the page count is what
  it will charge) and uploads it where the model can read it.
- ``start`` begins the translation of a prepared file and reserves the pages.
  A balance that does not cover them is `InsufficientBalanceError`, and
  nothing has been charged.
- ``status`` is asked until the job ends; ``result`` is then the translated
  file, which is HTML.

As in ``suliko_backend.py``, a suliko.ge that is down or misconfigured is
`SulikoUnavailableError`, never a verdict about the person or the file.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Protocol
from urllib.parse import quote, unquote

import httpx
import structlog
from fastapi import status as http_status

from suliko.config import get_settings
from suliko.core.errors import AppError, ValidationError
from suliko.integrations.suliko_backend import KEY_HEADER, SulikoUnavailableError

log = structlog.get_logger()

PREPARE_PATH = "/api/office/translate/prepare"
START_PATH = "/api/office/translate"
LANGUAGES_PATH = "/api/Language/public"

#: suliko.ge's `DocumentFormat`. Both come back as HTML; "rich" asks the model
#: to rebuild colours, tables and emphasis, which takes longer.
FORMAT_PLAIN_HTML = 5
FORMAT_RICH_HTML = 6

LANGUAGES_TTL_SECONDS = 600

JobState = Literal["processing", "completed", "failed"]


class InsufficientBalanceError(AppError):
    """The person's suliko.ge balance does not cover the document's pages."""

    status_code = http_status.HTTP_402_PAYMENT_REQUIRED
    error_code = "insufficient_balance"


class UnsupportedFileError(ValidationError):
    """A file suliko.ge cannot read (or one too large for it)."""

    error_code = "unsupported_file"


class SulikoAccountMissingError(AppError):
    """suliko.ge does not know the person Office named."""

    status_code = http_status.HTTP_409_CONFLICT
    error_code = "suliko_account_required"


@dataclass(frozen=True, slots=True)
class SulikoLanguage:
    id: int
    name: str
    name_geo: str


@dataclass(frozen=True, slots=True)
class PreparedFile:
    file_uri: str
    mime_type: str
    #: What suliko.ge measured, and so what it will charge.
    page_count: int
    #: The person's balance before this translation. None if it was not sent.
    balance: Decimal | None


@dataclass(frozen=True, slots=True)
class JobStatus:
    state: JobState
    progress: int
    #: suliko.ge's own words for a failure. Logged; shown only in summary.
    message: str | None = None


@dataclass(frozen=True, slots=True)
class TranslatedFile:
    content: bytes
    content_type: str
    file_name: str | None


class SulikoTranslator(Protocol):
    """What the translation routes need. The fake in the tests implements this."""

    @property
    def enabled(self) -> bool: ...

    async def languages(self) -> list[SulikoLanguage]: ...

    async def prepare(
        self, user_id: str, *, file_name: str, content_type: str, content: bytes
    ) -> PreparedFile: ...

    async def start(
        self,
        user_id: str,
        prepared: PreparedFile,
        *,
        file_name: str,
        target_language_id: int,
        source_language_id: int | None,
        output_language_id: int,
        rich: bool,
    ) -> str: ...

    async def status(self, job_id: str) -> JobStatus: ...

    async def result(self, job_id: str) -> TranslatedFile: ...

    async def aclose(self) -> None: ...


def _field(data: Any, name: str) -> Any:
    """A JSON field by name, whatever the casing the server serialises with."""
    if not isinstance(data, dict):
        return None
    wanted = name.lower()
    for key, value in data.items():
        if key.lower() == wanted:
            return value
    return None


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except (InvalidOperation, ValueError):
        return None


_FILENAME_STAR = re.compile(r"filename\*=(?:UTF-8'')?([^;]+)", re.IGNORECASE)
_FILENAME = re.compile(r'filename="?([^";]+)"?', re.IGNORECASE)


def _disposition_name(header: str | None) -> str | None:
    if not header:
        return None
    if match := _FILENAME_STAR.search(header):
        return unquote(match.group(1).strip()) or None
    if match := _FILENAME.search(header):
        return match.group(1).strip() or None
    return None


class HttpSulikoTranslator:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        # Preparing a file uploads it twice over (to suliko.ge, and from there
        # to the model's file store), so the limit is a long one.
        self._http = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=httpx.Timeout(timeout, connect=5.0)
        )
        self._languages: tuple[float, list[SulikoLanguage]] | None = None

    @property
    def enabled(self) -> bool:
        return True

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if not self._api_key:
            raise SulikoUnavailableError("Office is not set up to use Suliko Translate.")
        try:
            return await self._http.request(
                method, path, headers={KEY_HEADER: self._api_key}, **kwargs
            )
        except httpx.HTTPError as exc:
            log.error("suliko_translate_unreachable", error=type(exc).__name__, path=path)
            raise SulikoUnavailableError("Suliko Translate cannot be reached right now.") from exc

    def _unexpected(self, response: httpx.Response, path: str) -> SulikoUnavailableError:
        log.error("suliko_translate_status", status=response.status_code, path=path)
        return SulikoUnavailableError("Suliko Translate cannot be reached right now.")

    async def languages(self) -> list[SulikoLanguage]:
        now = time.monotonic()
        if self._languages and now - self._languages[0] < LANGUAGES_TTL_SECONDS:
            return self._languages[1]
        try:
            response = await self._http.get(LANGUAGES_PATH)
        except httpx.HTTPError as exc:
            log.error("suliko_translate_unreachable", error=type(exc).__name__, path=LANGUAGES_PATH)
            raise SulikoUnavailableError("Suliko Translate cannot be reached right now.") from exc
        data = _json(response)
        if response.status_code != 200 or not isinstance(data, list):
            raise self._unexpected(response, LANGUAGES_PATH)
        found = [
            SulikoLanguage(
                id=int(_field(row, "id")),
                name=str(_field(row, "name") or "").strip(),
                name_geo=str(_field(row, "nameGeo") or "").strip(),
            )
            for row in data
            if isinstance(_field(row, "id"), int)
        ]
        self._languages = (now, found)
        return found

    async def prepare(
        self, user_id: str, *, file_name: str, content_type: str, content: bytes
    ) -> PreparedFile:
        response = await self._send(
            "POST",
            PREPARE_PATH,
            data={"UserId": user_id},
            files={"File": (file_name, content, content_type)},
        )
        data = _json(response)
        error = _field(data, "error")
        if response.status_code == 404 and error == "user_not_found":
            raise SulikoAccountMissingError("Your suliko.ge account could not be found.")
        if response.status_code == 413 or error == "file_too_large":
            raise UnsupportedFileError("This file is too large for Suliko Translate.")
        if response.status_code == 400 and error in {"unsupported_file", "no_file"}:
            raise UnsupportedFileError("Suliko Translate cannot read this kind of file.")
        file_uri = _field(data, "fileUri")
        mime_type = _field(data, "mimeType")
        pages = _field(data, "pageCount")
        if (
            response.status_code != 200
            or not isinstance(file_uri, str)
            or not isinstance(mime_type, str)
            or not isinstance(pages, int)
        ):
            raise self._unexpected(response, PREPARE_PATH)
        return PreparedFile(
            file_uri=file_uri,
            mime_type=mime_type,
            page_count=max(1, pages),
            balance=_decimal(_field(data, "balance")),
        )

    async def start(
        self,
        user_id: str,
        prepared: PreparedFile,
        *,
        file_name: str,
        target_language_id: int,
        source_language_id: int | None,
        output_language_id: int,
        rich: bool,
    ) -> str:
        body: dict[str, Any] = {
            "userId": user_id,
            "fileUri": prepared.file_uri,
            "mimeType": prepared.mime_type,
            "fileName": file_name,
            "targetLanguageId": target_language_id,
            "outputLanguageId": output_language_id,
            "outputFormat": FORMAT_RICH_HTML if rich else FORMAT_PLAIN_HTML,
            "pageCount": prepared.page_count,
        }
        if source_language_id is not None:
            body["sourceLanguageId"] = source_language_id
        response = await self._send("POST", START_PATH, json=body)
        data = _json(response)
        error = _field(data, "error")
        if response.status_code == 402 or error == "insufficient_balance":
            raise InsufficientBalanceError(
                "Your suliko.ge balance does not cover this document.",
                pages=prepared.page_count,
                balance=str(_decimal(_field(data, "balance")) or prepared.balance or 0),
            )
        if response.status_code == 404 and error == "user_not_found":
            raise SulikoAccountMissingError("Your suliko.ge account could not be found.")
        if response.status_code == 400:
            # suliko.ge refused the file itself (a type its model cannot read).
            log.warning("suliko_translate_refused", body=response.text[:300])
            raise UnsupportedFileError("Suliko Translate cannot read this kind of file.")
        job_id = _field(data, "jobId")
        if response.status_code not in (200, 202) or not isinstance(job_id, str) or not job_id:
            raise self._unexpected(response, START_PATH)
        return job_id

    async def status(self, job_id: str) -> JobStatus:
        path = f"{START_PATH}/{quote(job_id, safe='')}/status"
        response = await self._send("GET", path)
        if response.status_code == 404:
            # suliko.ge keeps a job for a while and then forgets it.
            return JobStatus("failed", 0, "The job is no longer known to suliko.ge.")
        data = _json(response)
        state = str(_field(data, "status") or "").lower()
        if response.status_code != 200 or not state:
            raise self._unexpected(response, path)
        progress = _field(data, "progress")
        message = _field(data, "message")
        return JobStatus(
            state="completed"
            if state == "completed"
            else "failed"
            if state == "failed"
            else "processing",
            progress=progress if isinstance(progress, int) else 0,
            message=message if isinstance(message, str) else None,
        )

    async def result(self, job_id: str) -> TranslatedFile:
        path = f"{START_PATH}/{quote(job_id, safe='')}/result"
        response = await self._send("GET", path)
        if response.status_code != 200 or not response.content:
            raise self._unexpected(response, path)
        return TranslatedFile(
            content=response.content,
            content_type=response.headers.get("content-type", "text/html"),
            file_name=_disposition_name(response.headers.get("content-disposition")),
        )


class UnconfiguredSulikoTranslator:
    """No `SULIKO_API_URL`: every call says the service is not there."""

    @property
    def enabled(self) -> bool:
        return False

    async def aclose(self) -> None:
        return None

    def _off(self) -> SulikoUnavailableError:
        return SulikoUnavailableError("Suliko Translate is not set up on this server.")

    async def languages(self) -> list[SulikoLanguage]:
        raise self._off()

    async def prepare(
        self, user_id: str, *, file_name: str, content_type: str, content: bytes
    ) -> PreparedFile:
        raise self._off()

    async def start(
        self,
        user_id: str,
        prepared: PreparedFile,
        *,
        file_name: str,
        target_language_id: int,
        source_language_id: int | None,
        output_language_id: int,
        rich: bool,
    ) -> str:
        raise self._off()

    async def status(self, job_id: str) -> JobStatus:
        raise self._off()

    async def result(self, job_id: str) -> TranslatedFile:
        raise self._off()


_translator: SulikoTranslator | None = None


def get_suliko_translator() -> SulikoTranslator:
    """FastAPI dependency. Built once per process; tests pass their own."""
    global _translator
    if _translator is None:
        settings = get_settings()
        if settings.suliko_backend_enabled and settings.suliko_api_url:
            _translator = HttpSulikoTranslator(
                settings.suliko_api_url,
                settings.suliko_api_key.get_secret_value(),
                settings.suliko_translate_timeout_seconds,
            )
        else:
            _translator = UnconfiguredSulikoTranslator()
    return _translator


async def close_suliko_translator() -> None:
    global _translator
    if _translator is not None:
        await _translator.aclose()
    _translator = None
