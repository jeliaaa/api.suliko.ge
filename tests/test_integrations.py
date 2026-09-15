"""Integration credentials: the rules that must not regress.

The one that matters most is that a stored secret never leaves the API. Every
other property here is in service of that, or of not destroying a secret by
accident — the two ways this feature can do real damage.
"""

from __future__ import annotations

import json

import pytest

from suliko.api.v1.integrations import (
    PROVIDERS,
    IntegrationOut,
    _out,
    _stored_secrets,
)
from suliko.core.crypto import encrypt_for_tenant
from suliko.models.integration import IntegrationCredential, IntegrationProvider

TENANT = 1


class Row:
    """A credential row without a database."""

    def __init__(
        self,
        provider: IntegrationProvider,
        config: dict[str, object] | None = None,
        secrets: dict[str, str] | None = None,
        is_enabled: bool = False,
    ) -> None:
        self.provider = provider
        self.config = config or {}
        self.is_enabled = is_enabled
        self.secrets = (
            encrypt_for_tenant(TENANT, json.dumps(secrets)) if secrets is not None else None
        )
        self.secrets_updated_at = None
        self.secrets_updated_by_user_id = None
        self.last_check_at = None
        self.last_check_ok = None
        self.last_check_detail = None


# ── Every provider in the enum has a form ───────────────────────────────────


def test_every_provider_is_described() -> None:
    """A provider in the enum with no spec is a row nobody can fill in."""
    assert set(PROVIDERS) == set(IntegrationProvider)


@pytest.mark.parametrize("provider", list(IntegrationProvider))
def test_every_provider_has_at_least_one_field(provider: IntegrationProvider) -> None:
    assert PROVIDERS[provider].fields


@pytest.mark.parametrize("provider", list(IntegrationProvider))
def test_field_keys_are_unique_within_a_provider(provider: IntegrationProvider) -> None:
    """Two fields sharing a key means one silently overwrites the other."""
    keys = [f.key for f in PROVIDERS[provider].fields]
    assert len(keys) == len(set(keys))


def test_credentials_are_marked_secret() -> None:
    """Anything that grants access must be encrypted, not stored in config.

    Pinned by name because the cost of getting it wrong is a plaintext API key
    in a JSONB column that the audit log then copies.
    """
    must_be_secret = {
        (IntegrationProvider.GOOGLE_DRIVE, "service_account_json"),
        (IntegrationProvider.BOG_ECOMMERCE, "client_secret"),
        (IntegrationProvider.BOG_BUSINESS, "client_secret"),
        (IntegrationProvider.SMS_OFFICE, "api_key"),
        (IntegrationProvider.SMTP, "password"),
        (IntegrationProvider.ELEVENLABS, "api_key"),
        (IntegrationProvider.RECAPTCHA, "secret_key"),
        (IntegrationProvider.API24, "password"),
    }
    for provider, key in must_be_secret:
        field = next(f for f in PROVIDERS[provider].fields if f.key == key)
        assert field.secret, f"{provider.value}.{key} must be stored encrypted"


def test_the_recaptcha_site_key_is_not_secret() -> None:
    """It is rendered into the public page. Treating it as a secret would mean
    the frontend could never read it."""
    field = next(f for f in PROVIDERS[IntegrationProvider.RECAPTCHA].fields if f.key == "site_key")
    assert not field.secret


# ── Secrets never leave ─────────────────────────────────────────────────────


def test_the_response_carries_no_secret_values() -> None:
    row = Row(
        IntegrationProvider.SMTP,
        config={"host": "smtp.example.ge", "username": "office"},
        secrets={"password": "hunter2-the-real-password"},
    )
    out = _out(row, PROVIDERS[IntegrationProvider.SMTP], TENANT)  # type: ignore[arg-type]

    serialised = out.model_dump_json()
    assert "hunter2-the-real-password" not in serialised
    # Not even a fragment: a masked secret is a secret with less entropy.
    assert "hunter2" not in serialised
    # The NAME is reported, so the UI can say it is set.
    assert out.secrets_set == ["password"]


def test_a_provider_never_configured_reports_empty_not_null() -> None:
    out = _out(None, PROVIDERS[IntegrationProvider.SMTP], TENANT)
    assert isinstance(out, IntegrationOut)
    assert out.config == {}
    assert out.secrets_set == []
    assert out.is_enabled is False
    assert out.is_complete is False


# ── Completeness ────────────────────────────────────────────────────────────


def test_is_complete_requires_secrets_too() -> None:
    """Config alone is not enough — the thing that authenticates is the secret."""
    spec = PROVIDERS[IntegrationProvider.SMS_OFFICE]

    without = Row(IntegrationProvider.SMS_OFFICE, config={"sender": "SULIKO"}, secrets={})
    assert not _out(without, spec, TENANT).is_complete  # type: ignore[arg-type]

    with_key = Row(
        IntegrationProvider.SMS_OFFICE,
        config={"sender": "SULIKO"},
        secrets={"api_key": "abc123"},
    )
    assert _out(with_key, spec, TENANT).is_complete  # type: ignore[arg-type]


def test_optional_fields_do_not_block_completeness() -> None:
    spec = PROVIDERS[IntegrationProvider.GOOGLE_DRIVE]
    row = Row(
        IntegrationProvider.GOOGLE_DRIVE,
        config={},  # root_folder_id and impersonate_subject are optional
        secrets={"service_account_json": "{}"},
    )
    assert _out(row, spec, TENANT).is_complete  # type: ignore[arg-type]


def test_a_blank_string_does_not_count_as_set() -> None:
    """Whitespace in a required field is the commonest way a form looks filled
    in and is not."""
    spec = PROVIDERS[IntegrationProvider.SMS_OFFICE]
    row = Row(
        IntegrationProvider.SMS_OFFICE,
        config={"sender": "   "},
        secrets={"api_key": "abc123"},
    )
    assert not _out(row, spec, TENANT).is_complete  # type: ignore[arg-type]


# ── Decryption failure degrades, never explodes ─────────────────────────────


def test_an_undecryptable_blob_reads_as_no_secrets() -> None:
    """A changed master key must not take the Settings screen down — the fix
    is to re-enter the credentials, which needs the screen to load."""
    row = IntegrationCredential(
        provider=IntegrationProvider.SMTP,
        config={},
        secrets=b"not-a-valid-ciphertext",
    )
    assert _stored_secrets(row, TENANT) == {}


def test_secrets_round_trip_for_the_right_tenant_only() -> None:
    """The per-tenant key derivation is what stops one bureau's database row
    being readable in another bureau's context."""
    row = IntegrationCredential(
        provider=IntegrationProvider.SMTP,
        config={},
        secrets=encrypt_for_tenant(TENANT, json.dumps({"password": "s3cret"})),
    )
    assert _stored_secrets(row, TENANT) == {"password": "s3cret"}
    assert _stored_secrets(row, TENANT + 1) == {}
