"""Company records and invoice assembly.

Two things here can reach a client: an invoice with the wrong figures, and an
invoice that looks official but cannot be paid or filed. Both are pinned.
"""

from __future__ import annotations

from suliko.api.v1.companies import (
    INVOICE_REQUIRED,
    CompanyIn,
    InvoiceParty,
    _missing,
    _party,
)
from suliko.models.reference import Company


def company(**overrides: str) -> Company:
    base = {
        "role": "legal_entity",
        "name_ka": "შპს სულიკო",
        "name_en": "Suliko LLC",
        "director_ka": "დათა ხარაიშვილი",
        "director_en": "Data Kharaishvili",
        "site": "suliko.ge",
        "address_ka": "რუსთაველის 12, თბილისი",
        "address_en": "12 Rustaveli Ave, Tbilisi",
        "id_number": "404123456",
        "email": "office@suliko.ge",
        "phone": "+995 322 000 111",
        "whatsapp": "",
    }
    return Company(**{**base, **overrides})


# ── What an invoice cannot go out without ───────────────────────────────────


def test_a_complete_company_is_invoice_ready() -> None:
    assert _missing(company()) == []


def test_an_absent_company_is_missing_everything() -> None:
    """A tenant that has never filled the form in must not be one blank field
    away from issuing a document with no seller on it."""
    assert _missing(None) == list(INVOICE_REQUIRED)


def test_the_tax_number_is_required() -> None:
    """An invoice without the seller's registration number cannot be filed by
    the client's accountant, which is most of what an invoice is for."""
    assert _missing(company(id_number="")) == ["id_number"]


def test_whitespace_does_not_satisfy_a_required_field() -> None:
    """A space in the name field is the commonest way a form looks complete
    and is not."""
    assert _missing(company(name_ka="   ")) == ["name_ka"]


def test_the_english_name_is_not_required() -> None:
    """Georgian is the language of record. A bureau that never fills in the
    English side should still be able to invoice."""
    assert _missing(company(name_en="", address_en="", director_en="")) == []


# ── Locale fallback ─────────────────────────────────────────────────────────


def test_an_english_invoice_uses_the_english_name() -> None:
    party = _party(company(), "en")
    assert party.name == "Suliko LLC"
    assert party.address == "12 Rustaveli Ave, Tbilisi"


def test_a_georgian_invoice_uses_the_georgian_name() -> None:
    party = _party(company(), "ka")
    assert party.name == "შპს სულიკო"


def test_a_missing_translation_falls_back_rather_than_printing_blank() -> None:
    """A bureau that filled in only Georgian must not get an invoice with an
    empty seller line when a client asks for it in English."""
    party = _party(company(name_en="", address_en=""), "en")
    assert party.name == "შპს სულიკო"
    assert party.address == "რუსთაველის 12, თბილისი"


def test_no_company_yields_an_empty_party_rather_than_raising() -> None:
    """`_party` is reached only after the readiness check, but it must not be
    the thing that turns a misconfiguration into a 500."""
    party = _party(None, "ka")
    assert isinstance(party, InvoiceParty)
    assert party.name == ""


# ── The form accepts what the model stores ──────────────────────────────────


def test_every_company_field_is_optional_on_input() -> None:
    """The form is saved incrementally — someone fills in the name, saves, and
    comes back for the bank details. Requiring everything at once would make
    that impossible."""
    assert CompanyIn().model_dump() == dict.fromkeys(CompanyIn.model_fields, "")


def test_the_input_schema_covers_the_stored_columns() -> None:
    """A column the form cannot set is a column nobody can ever fill in."""
    stored = {
        c.name
        for c in Company.__table__.columns
        if c.name not in {"id", "tenant_id", "role", "created_at", "updated_at"}
    }
    assert stored == set(CompanyIn.model_fields)
