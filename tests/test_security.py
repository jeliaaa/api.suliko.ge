"""Password hashing, TOTP and envelope encryption.

No database needed — these are pure functions, and they are where the
account-takeover risk lives.
"""

from __future__ import annotations

import time

import pytest

from suliko.core.crypto import (
    DecryptionError,
    decrypt_for_tenant,
    encrypt_for_tenant,
    redact,
)
from suliko.security import totp as totp_service
from suliko.security.passwords import (
    generate_token,
    hash_password,
    hash_token,
    needs_rehash,
    validate_password_strength,
    verify_and_maybe_rehash,
    verify_password,
)

# ── Passwords ───────────────────────────────────────────────────────────────


def test_hash_is_argon2id() -> None:
    assert hash_password("correct horse battery staple").startswith("$argon2id$")


def test_hashes_are_salted() -> None:
    a = hash_password("same-password-twice")
    b = hash_password("same-password-twice")
    assert a != b, "identical passwords must not produce identical hashes"
    assert verify_password("same-password-twice", a)
    assert verify_password("same-password-twice", b)


def test_verify_rejects_wrong_password() -> None:
    hashed = hash_password("the-real-password")
    assert not verify_password("not-the-password", hashed)


def test_verify_rejects_garbage_hash_without_raising() -> None:
    """A corrupted hash column must fail closed, not 500."""
    for garbage in ("", "not-a-hash", "$argon2id$broken", "$2b$nope"):
        assert verify_password("anything", garbage) is False


def test_legacy_bcrypt_verifies_and_upgrades() -> None:
    """The whole PHP migration path: users upgrade as they sign in."""
    import bcrypt

    password = "legacy-php-password"
    legacy = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=10)).decode()

    assert verify_password(password, legacy)
    assert needs_rehash(legacy)

    ok, new_hash = verify_and_maybe_rehash(password, legacy)
    assert ok
    assert new_hash is not None
    assert new_hash.startswith("$argon2id$")
    assert verify_password(password, new_hash)


def test_argon2_hash_does_not_need_rehash() -> None:
    ok, new_hash = verify_and_maybe_rehash("pw", hash_password("pw"))
    assert ok
    assert new_hash is None


def test_failed_verification_offers_no_rehash() -> None:
    ok, new_hash = verify_and_maybe_rehash("wrong", hash_password("right"))
    assert not ok
    assert new_hash is None


def test_password_strength_requires_twelve_characters() -> None:
    assert validate_password_strength("short") != []
    assert validate_password_strength("a" * 12) == []


def test_common_passwords_rejected() -> None:
    assert validate_password_strength("password123") != []


# ── Opaque tokens ───────────────────────────────────────────────────────────


def test_tokens_are_unique_and_long() -> None:
    tokens = {generate_token() for _ in range(500)}
    assert len(tokens) == 500
    assert all(len(t) >= 43 for t in tokens), "expect ~256 bits of entropy"


def test_token_prefix_is_preserved() -> None:
    assert generate_token("sk_").startswith("sk_")


def test_token_hash_is_stable_and_opaque() -> None:
    token = generate_token()
    assert hash_token(token) == hash_token(token)
    assert len(hash_token(token)) == 64
    assert token not in hash_token(token)


# ── TOTP ────────────────────────────────────────────────────────────────────


def test_valid_code_is_accepted() -> None:
    import pyotp

    secret = totp_service.generate_secret()
    code = pyotp.TOTP(secret).now()

    result = totp_service.verify_code(secret, code)
    assert result.ok
    assert result.timestep == totp_service.current_timestep()


def test_wrong_code_is_rejected() -> None:
    secret = totp_service.generate_secret()
    assert not totp_service.verify_code(secret, "000000").ok


@pytest.mark.parametrize("bad", ["", "12345", "1234567", "abcdef", "12 34 56 78"])
def test_malformed_codes_are_rejected(bad: str) -> None:
    secret = totp_service.generate_secret()
    assert not totp_service.verify_code(secret, bad).ok


def test_replay_of_the_same_step_is_rejected() -> None:
    """The check most TOTP implementations omit.

    Without it a phished code stays valid for its whole 30-second window,
    which is exactly the window a real-time phishing proxy operates in.
    """
    import pyotp

    secret = totp_service.generate_secret()
    code = pyotp.TOTP(secret).now()

    first = totp_service.verify_code(secret, code)
    assert first.ok
    assert first.timestep is not None

    with pytest.raises(totp_service.ReplayError):
        totp_service.verify_code(secret, code, last_used_timestep=first.timestep)


def test_code_from_an_older_step_is_rejected_as_replay() -> None:
    import pyotp

    secret = totp_service.generate_secret()
    now = time.time()
    previous_code = pyotp.TOTP(secret).at(now - totp_service.TOTP_INTERVAL)

    # Still inside the drift window, but its step is already spent.
    with pytest.raises(totp_service.ReplayError):
        totp_service.verify_code(
            secret,
            previous_code,
            last_used_timestep=totp_service.current_timestep(now),
            at=now,
        )


def test_drift_window_accepts_neighbouring_steps() -> None:
    import pyotp

    secret = totp_service.generate_secret()
    now = time.time()

    for offset in (-totp_service.TOTP_INTERVAL, 0, totp_service.TOTP_INTERVAL):
        code = pyotp.TOTP(secret).at(now + offset)
        assert totp_service.verify_code(secret, code, at=now).ok


def test_drift_window_is_not_wider_than_one_step() -> None:
    """A wider window meaningfully helps an attacker holding a stale code."""
    import pyotp

    secret = totp_service.generate_secret()
    now = time.time()
    far = pyotp.TOTP(secret).at(now + 3 * totp_service.TOTP_INTERVAL)
    assert not totp_service.verify_code(secret, far, at=now).ok


def test_provisioning_uri_contains_issuer_and_account() -> None:
    secret = totp_service.generate_secret()
    uri = totp_service.provisioning_uri(secret, "tako@suliko.ge", "Suliko CRM")
    assert uri.startswith("otpauth://totp/")
    assert "Suliko%20CRM" in uri
    assert secret in uri


def test_recovery_codes_are_unique_and_formatted() -> None:
    codes = totp_service.generate_recovery_codes()
    assert len(codes) == totp_service.RECOVERY_CODE_COUNT
    assert len(set(codes)) == len(codes)
    assert all(len(c) == 11 and c[5] == "-" for c in codes)


def test_recovery_code_normalisation_is_forgiving() -> None:
    assert totp_service.normalise_recovery_code("  AB12C-DE34F  ") == "ab12c-de34f"


# ── Envelope encryption ─────────────────────────────────────────────────────


def test_encrypt_decrypt_round_trip() -> None:
    secret = "JBSWY3DPEHPK3PXP"
    blob = encrypt_for_tenant(1, secret)
    assert decrypt_for_tenant(1, blob) == secret


def test_ciphertext_does_not_contain_the_plaintext() -> None:
    blob = encrypt_for_tenant(1, "super-secret-value")
    assert b"super-secret-value" not in blob


def test_encryption_is_nondeterministic() -> None:
    """A fresh nonce per call, so identical secrets do not produce identical
    ciphertexts — otherwise the database reveals which tenants share a value."""
    a = encrypt_for_tenant(1, "same")
    b = encrypt_for_tenant(1, "same")
    assert a != b


def test_another_tenants_key_cannot_decrypt() -> None:
    blob = encrypt_for_tenant(1, "tenant-one-secret")
    with pytest.raises(DecryptionError):
        decrypt_for_tenant(2, blob)


def test_tampering_is_detected() -> None:
    """AES-GCM is authenticated: a flipped bit fails rather than decrypting
    to attacker-influenced plaintext."""
    blob = bytearray(encrypt_for_tenant(1, "do-not-tamper"))
    blob[-1] ^= 0x01
    with pytest.raises(DecryptionError):
        decrypt_for_tenant(1, bytes(blob))


def test_truncated_ciphertext_is_rejected() -> None:
    with pytest.raises(DecryptionError):
        decrypt_for_tenant(1, b"short")


def test_redaction_strips_secret_fields() -> None:
    cleaned = redact(
        {
            "name": "Tako",
            "password": "hunter2",
            "api_key": "tnb_abc",
            "nested": {"access_token": "xyz", "keep": "visible"},
        }
    )
    assert cleaned["name"] == "Tako"
    assert cleaned["password"] == "[redacted]"
    assert cleaned["api_key"] == "[redacted]"
    assert cleaned["nested"] == {"access_token": "[redacted]", "keep": "visible"}  # type: ignore[comparison-overlap]


# ── Production configuration guards ─────────────────────────────────────────


def _prod_settings(**overrides: object):  # type: ignore[no-untyped-def]
    from suliko.config import Settings

    base: dict[str, object] = {
        "environment": "production",
        "debug": False,
        "db_echo": False,
        "encryption_master_key": "a" * 44,
        "redis_url": "redis://localhost:6379/0",
        "cors_origins": ["https://app.suliko.ge"],
        # Auth mail must be deliverable in production: password reset answers
        # 204 either way, so an unconfigured mailer silently strands people.
        "smtp_host": "smtp.example.com",
        "smtp_from_email": "noreply@suliko.ge",
        "app_url": "https://app.suliko.ge",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_production_accepts_a_correct_configuration() -> None:
    _prod_settings().validate_for_production()


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"encryption_master_key": ""}, "ENCRYPTION_MASTER_KEY"),
        ({"debug": True}, "DEBUG"),
        ({"db_echo": True}, "DB_ECHO"),
        ({"redis_url": None}, "REDIS_URL"),
        ({"cors_origins": ["http://crm.example.com"]}, "https"),
        # Password reset answers 204 whether or not the account exists, so a
        # production box with no mailer tells every locked-out user that their
        # link is on its way and then drops it.
        ({"smtp_host": None}, "SMTP_HOST"),
        ({"smtp_from_email": None}, "SMTP_FROM_EMAIL"),
        # Reset links are clicked from an email client, off our network.
        ({"app_url": "http://app.suliko.ge"}, "APP_URL"),
    ],
)
def test_production_refuses_insecure_configuration(
    overrides: dict[str, object], expected: str
) -> None:
    with pytest.raises(RuntimeError, match=expected):
        _prod_settings(**overrides).validate_for_production()


def test_single_instance_flag_permits_running_without_redis() -> None:
    """Redis has no supported native Windows build, and a one-worker box on
    IIS is a real deployment target. The flag is an explicit acknowledgement,
    not a way to silence the check."""
    _prod_settings(redis_url=None, rate_limit_single_instance=True).validate_for_production()


@pytest.mark.parametrize("origin", ["http://localhost:3000", "http://127.0.0.1:3000"])
def test_loopback_cors_origins_are_allowed_in_production(origin: str) -> None:
    """Loopback never leaves the machine, so it is not an insecure transport.
    IIS reverse-proxying to 127.0.0.1 is the normal single-box topology."""
    _prod_settings(cors_origins=[origin]).validate_for_production()
