"""Starter reference data for a new organisation.

Shared by `suliko seed-reference` and self-signup, so an organisation created
through the website starts with the same catalogues as one created on the
server.

## What is seeded, and what deliberately is not

Languages and document types are catalogues: which ones exist is not a
business decision, and a bureau that cannot pick "Passport" from a list cannot
create its first order at all.

Prices ARE a business decision, so self-signup does not seed them. The
starter rates below were read off a scrolled screenshot of one bureau's
Calculator — an incomplete subset that does not even include ka→en — and
quoting a new organisation's clients at somebody else's partial rate card
would be worse than making them set their own. They remain available to the
CLI, behind an explicit flag and a warning.

Every function here expects the session to be scoped to the target tenant
already.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.models.reference import DocumentType, Language, LanguagePairPrice

#: Language set the bureau actually works in, taken from the PHP's
#: API24_LANG_IDS map in config.php.
LANGUAGES: tuple[tuple[str, str, str], ...] = (
    ("ka", "Georgian", "ქართული"),
    ("en", "English", "ინგლისური"),
    ("ru", "Russian", "რუსული"),
    ("de", "German", "გერმანული"),
    ("fr", "French", "ფრანგული"),
    ("it", "Italian", "იტალიური"),
    ("es", "Spanish", "ესპანური"),
    ("pt", "Portuguese", "პორტუგალიური"),
    ("tr", "Turkish", "თურქული"),
    ("az", "Azerbaijani", "აზერბაიჯანული"),
    ("hy", "Armenian", "სომხური"),
    ("uk", "Ukrainian", "უკრაინული"),
    ("pl", "Polish", "პოლონური"),
    ("ar", "Arabic", "არაბული"),
    ("he", "Hebrew", "ებრაული"),
    ("zh", "Chinese", "ჩინური"),
    ("ja", "Japanese", "იაპონური"),
    ("el", "Greek", "ბერძნული"),
    ("lv", "Latvian", "ლატვიური"),
    ("sl", "Slovenian", "სლოვენური"),
    ("sk", "Slovak", "სლოვაკური"),
    ("sr", "Serbian", "სერბული"),
    ("ur", "Urdu", "ურდუ"),
    ("fi", "Finnish", "ფინური"),
    ("la", "Latin", "ლათინური"),
)

#: Starter document types. Multipliers all 1.0 — set your own in Settings.
DOCUMENT_TYPES: tuple[tuple[str, str], ...] = (
    ("Passport", "პასპორტი"),
    ("ID card", "პირადობის მოწმობა"),
    ("Birth certificate", "დაბადების მოწმობა"),
    ("Marriage certificate", "ქორწინების მოწმობა"),
    ("Diploma", "დიპლომი"),
    ("Transcript", "ნიშნების ფურცელი"),
    ("Certificate", "ცნობა"),
    ("Power of attorney", "მინდობილობა"),
    ("Contract", "ხელშეკრულება"),
    ("Letter", "წერილი"),
    ("Technical specifications", "ტექნიკური მახასიათებლები"),
    ("Medical record", "სამედიცინო ჩანაწერი"),
    ("Bank statement", "საბანკო ამონაწერი"),
    ("Court document", "სასამართლო დოკუმენტი"),
    ("Other", "სხვა"),
)

#: Rates read off the production Calculator screen's "Pricing Reference" panel.
#: The panel was scrolled, so this is the visible subset — NOT a complete rate
#: card. Verify every line against your own before quoting a client.
STARTER_RATES: tuple[tuple[str, str, str], ...] = (
    ("az", "da", "100.00"),
    ("ka", "sv", "60.00"),
    ("sv", "ka", "60.00"),
    ("es", "ka", "50.00"),
    ("pt", "ka", "50.00"),
    ("ka", "uk", "45.00"),
    ("pt", "ru", "45.00"),
    ("ru", "pt", "45.00"),
    ("uk", "ka", "45.00"),
    ("en", "es", "40.00"),
    ("en", "fr", "40.00"),
    ("en", "it", "40.00"),
    ("en", "pt", "40.00"),
    ("es", "en", "40.00"),
    ("es", "ru", "40.00"),
    ("fr", "en", "40.00"),
    ("fr", "ru", "40.00"),
    ("it", "en", "40.00"),
    ("it", "ru", "40.00"),
    ("ka", "es", "40.00"),
    ("ka", "pt", "40.00"),
    ("pt", "en", "40.00"),
)


@dataclass(frozen=True, slots=True)
class SeedResult:
    languages_added: int
    languages_present: int
    document_types_added: int
    document_types_present: int
    rates_added: int


async def seed_reference_data(db: AsyncSession, *, with_rates: bool = False) -> SeedResult:
    """Add whatever of the starter catalogues is missing. Idempotent.

    Existing rows are never touched, so running it on an organisation that has
    already edited its own list only fills gaps.
    """
    existing_codes = set((await db.execute(select(Language.code))).scalars())
    languages_added = 0
    for code, name_en, name_ka in LANGUAGES:
        if code not in existing_codes:
            db.add(Language(code=code, name_en=name_en, name_ka=name_ka))
            languages_added += 1

    existing_types = set((await db.execute(select(DocumentType.name_en))).scalars())
    types_added = 0
    for name_en, name_ka in DOCUMENT_TYPES:
        if name_en not in existing_types:
            db.add(DocumentType(name_en=name_en, name_ka=name_ka, price_multiplier=Decimal("1.0")))
            types_added += 1

    rates_added = 0
    if with_rates:
        existing_pairs = {
            (src, tgt)
            for src, tgt in await db.execute(
                select(LanguagePairPrice.source_language, LanguagePairPrice.target_language)
            )
        }
        for src, tgt, price in STARTER_RATES:
            if (src, tgt) not in existing_pairs:
                db.add(
                    LanguagePairPrice(
                        source_language=src,
                        target_language=tgt,
                        price_per_page=Decimal(price),
                        is_active=True,
                    )
                )
                rates_added += 1

    await db.flush()
    return SeedResult(
        languages_added=languages_added,
        languages_present=len(existing_codes),
        document_types_added=types_added,
        document_types_present=len(existing_types),
        rates_added=rates_added,
    )
