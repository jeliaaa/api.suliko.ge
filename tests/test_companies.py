"""Company records and invoice assembly.

Two things here can reach a client: an invoice with the wrong figures, and an
invoice that looks official but cannot be paid or filed. Both are pinned.
"""

from __future__ import annotations

from sqlalchemy.dialects import postgresql

from suliko.api.v1.companies import (
    INVOICE_REQUIRED,
    CompanyIn,
    InvoiceParty,
    _invoice_documents_query,
    _language_pair,
    _missing,
    _party,
)
from suliko.models.reference import Company, Language


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


def test_the_director_signs_in_the_invoice_language() -> None:
    assert _party(company(), "ka").director == "დათა ხარაიშვილი"
    assert _party(company(), "en").director == "Data Kharaishvili"


def test_a_director_without_an_english_name_still_signs_an_english_invoice() -> None:
    assert _party(company(director_en=""), "en").director == "დათა ხარაიშვილი"


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


# ── The document lines ──────────────────────────────────────────────────────


def test_invoice_lines_load_the_document_type_in_the_same_query() -> None:
    """Every invoice line prints its document type's name.

    `OrderDocument.document_type` is declared lazy="raise", so a missing eager
    load is not a slow invoice — it is no invoice at all, raising on the first
    line and turning `GET /orders/{id}/invoice` into a 500. The join is the
    fix, and this is the test that the join is still there.
    """
    sql = str(_invoice_documents_query(7).compile(dialect=postgresql.dialect()))

    assert "JOIN document_types" in sql
    assert "document_types_1.name_ka" in sql
    assert "document_types_1.name_en" in sql


# ── Languages by name ───────────────────────────────────────────────────────


LANGUAGES = {
    "ka": Language(code="ka", name_ka="ქართული", name_en="Georgian"),
    "en": Language(code="en", name_ka="ინგლისური", name_en="English"),
}


def test_a_georgian_invoice_names_the_languages_in_georgian() -> None:
    """A client reads "ქართული → ინგლისური", not the codes "KA → EN"."""
    assert _language_pair("ka", "en", LANGUAGES, "ka") == "ქართული → ინგლისური"


def test_an_english_invoice_names_the_languages_in_english() -> None:
    assert _language_pair("ka", "en", LANGUAGES, "en") == "Georgian → English"


def test_a_language_without_an_english_name_falls_back_to_georgian() -> None:
    names = {**LANGUAGES, "de": Language(code="de", name_ka="გერმანული", name_en="")}
    assert _language_pair("de", "en", names, "en") == "გერმანული → English"


def test_a_language_no_longer_on_the_list_prints_its_code() -> None:
    """Removed from the bureau's list after the order was taken: the line
    still says which language it was."""
    assert _language_pair("ka", "tr", LANGUAGES, "ka") == "ქართული → TR"
