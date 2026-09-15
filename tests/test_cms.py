"""Service-page and site-string validation.

The CMS writes content to a public website, so the rules that matter are the
ones about URLs and locales — the two things a mistake in makes visible to
everyone outside the company.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticError

from suliko.api.v1.service_pages import (
    LOCALES,
    PageCreate,
    PageUpdate,
    StringIn,
)
from suliko.models.cms import PageStatus


def _page(**overrides: object) -> PageCreate:
    payload: dict[str, object] = {
        "slug": "notarised-translation",
        "locale": "ka",
        "title": "ნოტარიული თარგმანი",
    }
    payload.update(overrides)
    return PageCreate.model_validate(payload)


# ── Slugs ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "slug",
    ["services", "notarised-translation", "apostille-2026", "a-b-c"],
)
def test_valid_slugs(slug: str) -> None:
    assert _page(slug=slug).slug == slug


@pytest.mark.parametrize(
    "slug",
    [
        "notarised translation",  # space
        "notarised--translation",  # doubled hyphen
        "-leading",
        "trailing-",
        "under_score",
        "услуги",  # non-ASCII: fine in a title, not in a URL segment
    ],
)
def test_invalid_slugs_are_rejected(slug: str) -> None:
    with pytest.raises(PydanticError, match="lowercase words"):
        _page(slug=slug)


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("  services  ", "services"),  # whitespace from a paste
        ("Notarised-Translation", "notarised-translation"),  # capitals from a title
    ],
)
def test_a_slug_is_normalised_rather_than_rejected(given: str, stored: str) -> None:
    """These are typos, not different pages. Rejecting them would send someone
    back to the form to retype what they clearly meant; normalising also means
    two editors cannot create `Services` and `services` as separate URLs."""
    assert _page(slug=given).slug == stored


# ── Locales ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("locale", LOCALES)
def test_published_locales_are_accepted(locale: str) -> None:
    assert _page(locale=locale).locale == locale


def test_an_unpublished_locale_is_rejected() -> None:
    """A page in a locale the site does not serve is invisible content that
    still shows up in the editor's "missing translations" list forever."""
    with pytest.raises(PydanticError, match="Locale must be one of"):
        _page(locale="ru")


# ── Update shape ────────────────────────────────────────────────────────────


def test_update_cannot_change_the_slug() -> None:
    """A published slug is a URL someone has linked to. Renaming it is a
    redirect decision, not an edit-form side effect."""
    assert "slug" not in PageUpdate.model_fields
    assert "locale" not in PageUpdate.model_fields

    with pytest.raises(PydanticError):
        PageUpdate.model_validate({"slug": "something-else"})


def test_update_leaves_unset_fields_alone() -> None:
    """`exclude_unset` is what makes a partial update partial — without it,
    editing the title would blank the body."""
    patch = PageUpdate.model_validate({"title": "New title"})
    assert patch.model_dump(exclude_unset=True) == {"title": "New title"}


def test_a_new_page_starts_as_a_draft() -> None:
    """Publishing must be a deliberate act — otherwise a half-written page is
    live the moment it is created."""
    assert _page().status is PageStatus.DRAFT


# ── Site strings ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("key", ["nav.services", "footer.legal.privacy", "tagline", "cta_button"])
def test_valid_string_keys(key: str) -> None:
    assert StringIn(key=key, locale="en", value="x").key == key


@pytest.mark.parametrize("key", ["nav services", "nav/services", "nav:services", ""])
def test_invalid_string_keys_are_rejected(key: str) -> None:
    with pytest.raises(PydanticError):
        StringIn(key=key, locale="en", value="x")


def test_an_empty_value_is_allowed() -> None:
    """Deliberate: an empty string is a key someone added but has not written
    yet, and the editor's "untranslated" filter is built to find exactly
    those. Rejecting it would mean the gap could not be recorded at all."""
    assert StringIn(key="nav.services", locale="en", value="").value == ""
