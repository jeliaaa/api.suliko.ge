"""Every language pair a translator works in now runs both ways.

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-14

Decided 2026-10-14: a translator who works from Georgian into English also
works from English into Georgian. New and edited pairs are saved with their
reverse (``domain/translator_pairs.py``); this adds the reverse of every pair
that is already stored without one, so the translators a bureau entered before
today behave the same way.

Data only, no schema change. The reverse is added with no rate of its own (an
empty rate means "the one that is set": the screens show the forward
direction's rate for it). A pair whose reverse already exists is left alone,
so nothing is duplicated and an own reverse rate is kept.

The table has FORCE ROW LEVEL SECURITY, which binds the owner too, so, as 0011
and 0016 do, the backfill walks the tenants and sets ``suliko.tenant_id`` for
each.

## Downgrade

Nothing to undo: the added rows cannot be told from rows a bureau saved
itself, and a pair with its reverse is still a valid set of pairs.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | None = None
depends_on: str | None = None

#: No tables. Listed so the parity tests' chain checks see this revision.
NEW_TABLES: tuple[str, ...] = ()

BACKFILL = """
DO $$
DECLARE
    t RECORD;
BEGIN
    FOR t IN SELECT id FROM tenants ORDER BY id LOOP
        PERFORM set_config('suliko.tenant_id', t.id::text, true);
        INSERT INTO translator_language_pairs
            (tenant_id, translator_id, source_language, target_language, rate_per_page)
        SELECT p.tenant_id, p.translator_id, p.target_language, p.source_language, NULL
        FROM translator_language_pairs AS p
        WHERE p.tenant_id = t.id
          AND NOT EXISTS (
              SELECT 1
              FROM translator_language_pairs AS r
              WHERE r.tenant_id = p.tenant_id
                AND r.translator_id = p.translator_id
                AND r.source_language = p.target_language
                AND r.target_language = p.source_language
          )
        GROUP BY p.tenant_id, p.translator_id, p.source_language, p.target_language
        ORDER BY min(p.id);
    END LOOP;
    PERFORM set_config('suliko.tenant_id', '', true);
END $$;
"""


def upgrade() -> None:
    op.execute(sa.text(BACKFILL))


def downgrade() -> None:
    """Nothing to undo: see the module docstring."""
