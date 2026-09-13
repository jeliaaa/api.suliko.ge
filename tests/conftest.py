"""Shared test configuration.

Settings are pinned here rather than read from ``.env`` so the suite is
hermetic: a developer's local database URL or a missing key must not change
whether tests pass.
"""

from __future__ import annotations

import base64
import os

# Must be set before suliko.config is imported anywhere, because Settings is
# an lru_cache'd singleton built from the environment at first access.
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault(
    "ENCRYPTION_MASTER_KEY",
    # Deterministic, test-only, and obviously not a real key.
    base64.urlsafe_b64encode(b"suliko-test-key-do-not-use-32byt").decode(),
)
os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://suliko:suliko@localhost:5432/suliko_test",
    ),
)
# No Redis in tests: the in-process limiter is deterministic and needs no
# service. Production refuses to start this way — see Settings.validate_for_production.
os.environ.pop("REDIS_URL", None)

import pytest

from suliko.config import get_settings


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    """Rate-limit counters are process-global; stop them leaking between tests."""
    from suliko.core.ratelimit import reset_rate_limiter

    reset_rate_limiter()


@pytest.fixture(scope="session")
def settings():  # type: ignore[no-untyped-def]
    return get_settings()
