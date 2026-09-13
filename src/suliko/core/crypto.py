"""Envelope encryption for secrets stored at rest.

Used for TOTP secrets and per-tenant integration credentials (Bank of Georgia,
Google Drive, SMS, API24). The PHP app stores all of these in the clear —
``api24_tokens.api24_password`` is plain text by design so it can be replayed
to the provider's login endpoint. That does not survive the port.

## Scheme

    master key (env / KMS)
      -> per-tenant data key, wrapped by the master key, stored in the DB
        -> AES-256-GCM over the plaintext

Per-tenant data keys mean a compromise scoped to one tenant does not decrypt
another's, and rotating the master key rewraps N small keys rather than
re-encrypting every ciphertext.

AES-GCM is authenticated: tampering with a stored ciphertext fails to decrypt
rather than yielding attacker-chosen plaintext. The tenant id is bound in as
associated data, so a ciphertext moved between tenant rows will not decrypt.

## What is NOT here

Key rotation and a real KMS. The wrapped-key table carries a ``key_version``
so rotation is additive when it lands. Until then the master key is a single
env var, which is acceptable for launch and must not be for long.
"""

from __future__ import annotations

import base64
import hashlib
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from suliko.config import get_settings

NONCE_BYTES = 12  # 96 bits, the GCM standard.
KEY_BYTES = 32  # AES-256.


class DecryptionError(Exception):
    """Ciphertext failed authentication or the key is wrong."""


def _master_key() -> bytes:
    raw = get_settings().encryption_master_key.get_secret_value()
    if not raw:
        raise RuntimeError(
            "ENCRYPTION_MASTER_KEY is not set. Generate one with:\n"
            '  python -c "import os,base64;'
            'print(base64.urlsafe_b64encode(os.urandom(32)).decode())"'
        )
    key = base64.urlsafe_b64decode(raw)
    if len(key) != KEY_BYTES:
        raise RuntimeError("ENCRYPTION_MASTER_KEY must decode to exactly 32 bytes.")
    return key


def _tenant_key(tenant_id: int) -> bytes:
    """Derive this tenant's data key from the master key.

    HKDF-style derivation rather than a stored wrapped key, for now: it needs
    no extra table and no bootstrap step, and rotating the master key rotates
    every tenant key with it. The trade is that rotation re-encrypts
    everything rather than rewrapping — acceptable at this volume (TOTP
    secrets and a handful of integration credentials per tenant), and the
    point at which it stops being acceptable is the point to introduce the
    stored-key table.
    """
    return hashlib.blake2b(
        f"tenant:{tenant_id}".encode(),
        key=_master_key(),
        digest_size=KEY_BYTES,
    ).digest()


def encrypt_for_tenant(tenant_id: int, plaintext: str) -> bytes:
    """Encrypt a secret. Output is ``nonce || ciphertext || tag``."""
    aes = AESGCM(_tenant_key(tenant_id))
    nonce = os.urandom(NONCE_BYTES)
    # Binding the tenant id as associated data means a ciphertext copied into
    # another tenant's row will not decrypt, even with the same master key.
    aad = str(tenant_id).encode()
    ciphertext = aes.encrypt(nonce, plaintext.encode("utf-8"), aad)
    return nonce + ciphertext


def decrypt_for_tenant(tenant_id: int, blob: bytes) -> str:
    if len(blob) <= NONCE_BYTES:
        raise DecryptionError("Ciphertext is too short to be valid.")

    aes = AESGCM(_tenant_key(tenant_id))
    nonce, ciphertext = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    aad = str(tenant_id).encode()

    try:
        return aes.decrypt(nonce, ciphertext, aad).decode("utf-8")
    except InvalidTag as exc:
        # Never include the ciphertext or the key in the message.
        raise DecryptionError("Could not decrypt: wrong key or tampered data.") from exc


#: Field names whose values are stripped before anything is logged or written
#: to the audit log's before/after documents.
SECRET_FIELD_NAMES = frozenset(
    {
        "password",
        "password_hash",
        "portal_password_hash",
        "secret",
        "secret_encrypted",
        "api_key",
        "api_key_hash",
        "access_token",
        "refresh_token",
        "token",
        "token_hash",
        "session_token",
        "code",
        "code_hash",
        "encryption_master_key",
        "api24_password",
        "client_secret",
        "bank_iban",
    }
)


def redact(data: dict[str, Any]) -> dict[str, Any]:
    """Replace secret-looking values with a marker, recursively."""
    result: dict[str, object] = {}
    for key, value in data.items():
        if key.lower() in SECRET_FIELD_NAMES:
            result[key] = "[redacted]"
        elif isinstance(value, dict):
            result[key] = redact(value)
        else:
            result[key] = value
    return result
