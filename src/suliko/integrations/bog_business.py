"""Bank of Georgia Business Online: outbound transfers that the owner then signs.

A transfer made through this API is only CREATED here. It lands in the
company's Business Online as a document "ready to sign" (result code 0) and no
money moves until a person with signing rights approves it in the bank's app.
That is the point: Suliko Office can prepare the payment, the owner confirms
it with their own signature, and Office never holds that right.

Three calls, from https://api.bog.ge/docs/en/bonline :

- token: ``POST account.bog.ge/.../openid-connect/token`` with the client id
  and secret (OAuth client credentials).
- create: ``POST api/documents/domestic`` takes a list of documents and
  answers with a list of ``{UniqueId, UniqueKey, ResultCode, Match}``.
- status: ``GET api/documents/status/{UniqueKey}``.

The docs do not list the values of ``Status``, so ``state_of`` reads it
conservatively: only words that clearly mean paid or refused change the
state, and anything else stays "pending" with the bank's own word shown.
Marking a transfer as paid by mistake is the failure that costs money.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Literal

import httpx
import structlog

from suliko.core.slow_requests import upstream_hooks

log = structlog.get_logger()

TOKEN_URL = "https://account.bog.ge/auth/realms/bog/protocol/openid-connect/token"  # noqa: S105 (a public address, not a secret)
API_URL = "https://api.businessonline.ge/api"

#: A "BULK" transfer is the cheap standard one and the bank caps it here;
#: above it the document has to go as an individual MT103 transfer.
BULK_LIMIT = Decimal("10000")

#: ResultCode 0 is the only success: "ready to sign".
READY_TO_SIGN = 0
#: A document with this UniqueId was created in the last 7 days.
DUPLICATE = 702

State = Literal["pending", "completed", "rejected"]

#: Whole words in the bank's Status that mean the money has moved / has not.
_DONE_WORDS = ("execut", "complet", "paid", "success", "settled", "processed")
_REFUSED_WORDS = ("reject", "declin", "cancel", "refus", "fail", "expire", "delet", "revok")

# A short, honest reading of the result codes a person can act on. Anything
# else is shown as the bank's number, which support can look up.
_RESULT_TEXT: dict[int, str] = {
    28: "The bank refused the tax ID check for this account.",
    29: "The notary's account number is not valid.",
    30: "The notary's account number or bank code is empty.",
    31: "The notary's bank code is not valid.",
    41: "The notary's tax ID is not valid.",
    52: "The notary's tax ID is not valid.",
    54: "The payment purpose is empty.",
    55: "The payment purpose is too long.",
    147: "The notary's IBAN has the wrong format.",
    333: "The account does not have enough money.",
    363: "The bank already has this document.",
    444: "The account does not have enough money.",
    666: "The payment purpose contains characters the bank does not accept.",
    674: "The bank does not allow a transfer to this account.",
    675: "The source account is not valid.",
    676: "This integration has no access to the source account.",
    677: "The payment purpose is empty.",
    678: "The transfer type is missing.",
    701: "The bank rejected the document's details.",
    702: "The bank already has a document with this id.",
    1001: "This integration has no right to make transfers.",
    1002: "The notary's bank details are not correct.",
}


class BogError(Exception):
    """A call to the bank failed.

    ``reached`` says whether the bank answered. A refusal it sent (reached) is
    certain: no document exists. A timeout or a dropped connection is not: the
    document may or may not have been created, so the caller must not assume
    either.
    """

    def __init__(self, message: str, *, reached: bool, result_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.reached = reached
        self.result_code = result_code


@dataclass(frozen=True, slots=True)
class BogCreated:
    unique_id: str
    unique_key: int
    result_code: int
    match: Decimal | None


@dataclass(frozen=True, slots=True)
class BogStatus:
    status: str
    reject_code: int | None
    result_code: int | None


@dataclass(frozen=True, slots=True)
class BogCredentials:
    client_id: str
    client_secret: str
    source_iban: str
    payer_inn: str
    payer_name: str


def describe_result(code: int | None) -> str:
    if code is None:
        return "The bank did not say why."
    return _RESULT_TEXT.get(code, f"The bank refused it (code {code}).")


def state_of(status: str | None, reject_code: int | None) -> State:
    """Pending, completed or rejected, from what the bank reported.

    A reject code, or a status that clearly says refused, wins; a status that
    clearly says paid completes it; everything else is still pending. The
    letters the old PHP system decoded are not in BOG's public docs, so they
    are not guessed at here.
    """
    word = (status or "").strip().lower()
    if reject_code or any(w in word for w in _REFUSED_WORDS):
        return "rejected"
    if any(w in word for w in _DONE_WORDS):
        return "completed"
    return "pending"


def _field(data: Any, name: str) -> Any:
    if not isinstance(data, dict):
        return None
    wanted = name.lower()
    for key, value in data.items():
        if key.lower() == wanted:
            return value
    return None


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class BogBusiness:
    """The bank's API for one organisation's credentials."""

    def __init__(
        self, credentials: BogCredentials, client: httpx.AsyncClient | None = None
    ) -> None:
        self._credentials = credentials
        self._http = client or httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=5.0),
            event_hooks=upstream_hooks("bog_business"),
        )
        self._token: tuple[float, str] | None = None

    @property
    def credentials(self) -> BogCredentials:
        return self._credentials

    async def aclose(self) -> None:
        await self._http.aclose()

    async def token(self) -> str:
        now = time.monotonic()
        if self._token and now < self._token[0]:
            return self._token[1]
        try:
            response = await self._http.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(self._credentials.client_id, self._credentials.client_secret),
            )
        except httpx.HTTPError as exc:
            log.error("bog_business_token_unreachable", error=type(exc).__name__)
            raise BogError("The bank cannot be reached right now.", reached=False) from exc
        body = _json(response)
        token = _field(body, "access_token")
        if response.status_code != 200 or not isinstance(token, str) or not token:
            log.warning("bog_business_token_refused", status=response.status_code)
            raise BogError("The bank did not accept the Client ID and Client secret.", reached=True)
        lifetime = _int(_field(body, "expires_in")) or 300
        self._token = (now + max(30, lifetime - 30), token)
        return token

    async def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        token = await self.token()
        try:
            return await self._http.request(
                method,
                f"{API_URL}/{path}",
                headers={"Authorization": f"Bearer {token}"},
                **kwargs,
            )
        except httpx.HTTPError as exc:
            log.error("bog_business_unreachable", error=type(exc).__name__, path=path)
            raise BogError("The bank cannot be reached right now.", reached=False) from exc

    async def check(self) -> None:
        """Sign in. Moves no money; proves the id and secret are the bank's."""
        await self.token()

    async def create_domestic(
        self,
        *,
        unique_id: uuid.UUID,
        document_no: str,
        amount: Decimal,
        nomination: str,
        value_date: date,
        beneficiary_iban: str,
        beneficiary_bank_code: str,
        beneficiary_inn: str,
        beneficiary_name: str | None,
    ) -> BogCreated:
        """One transfer in lari, left for the owner to sign in Business Online."""
        document = {
            "Nomination": nomination[:250],
            "DispatchType": "BULK" if amount <= BULK_LIMIT else "MT103",
            "ValueDate": value_date.isoformat(),
            "UniqueId": str(unique_id),
            # A second submit of the same document inside 7 days is refused
            # with 702 instead of becoming a second transfer.
            "CheckUniqueId": True,
            "Amount": float(amount),
            "DocumentNo": document_no[:16],
            "SourceAccountNumber": self._credentials.source_iban,
            "BeneficiaryAccountNumber": beneficiary_iban,
            "BeneficiaryBankCode": beneficiary_bank_code,
            "BeneficiaryInn": beneficiary_inn,
            "BeneficiaryName": beneficiary_name,
            # The payer's own tax id is sent only for third-party budget
            # transfers; for a notary it is the company paying for itself.
            "PayerInn": self._credentials.payer_inn,
            "PayerName": self._credentials.payer_name,
            "CheckInn": False,
        }
        response = await self._call("POST", "documents/domestic", json=[document])
        body = _json(response)
        if response.status_code in (401, 403):
            raise BogError("The bank did not accept this integration's access.", reached=True)
        if response.status_code >= 500:
            # The bank answered with a server error: the document may exist.
            raise BogError("The bank had an error and may not have received it.", reached=False)
        row = body[0] if isinstance(body, list) and body else None
        result_code = _int(_field(row, "ResultCode"))
        key = _int(_field(row, "UniqueKey"))
        if response.status_code != 200 or result_code is None:
            log.warning("bog_business_create_refused", status=response.status_code)
            raise BogError("The bank refused the transfer.", reached=True)
        if result_code != READY_TO_SIGN or key is None:
            raise BogError(describe_result(result_code), reached=True, result_code=result_code)
        match = _field(row, "Match")
        try:
            match_value = Decimal(str(match)) if match is not None else None
        except ArithmeticError:
            match_value = None
        return BogCreated(
            unique_id=str(_field(row, "UniqueId") or unique_id),
            unique_key=key,
            result_code=result_code,
            match=match_value,
        )

    async def status(self, unique_key: int) -> BogStatus:
        response = await self._call("GET", f"documents/status/{unique_key}")
        body = _json(response)
        if response.status_code != 200 or not isinstance(body, dict):
            log.warning("bog_business_status_refused", status=response.status_code)
            raise BogError("The bank could not report on this transfer.", reached=True)
        status = _field(body, "Status")
        return BogStatus(
            status=str(status) if status is not None else "",
            reject_code=_int(_field(body, "RejectCode")),
            result_code=_int(_field(body, "ResultCode")),
        )


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None
