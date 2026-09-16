"""Backfill tenants onto the bureau plan.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-16

Data only — no schema change. ``tenants.plan`` has existed since revision 0001
as an unused nullable column; this revision gives it a meaning and makes the
existing rows agree with it.

## Why this has to exist

With plans switched on, ``plan IS NULL`` means "signed up, has not chosen
yet": such a tenant is enforced as a FREELANCER and sent to onboarding. Every
tenant that predates this revision has a null plan and is a working bureau, so
without this backfill they would all wake up as freelancers — the Finances and
Users tabs gone, `users.manage` withheld, and a forced onboarding screen in
front of people who onboarded months ago.

Bureau rather than freelancer because it is the wider of the two: restoring a
tab someone should not have had is a support conversation, and removing one
they were using mid-invoice is an outage.

## Reversing

``downgrade`` puts the nulls back, which returns those tenants to "has not
chosen". That is the honest inverse — there is no record of which of them
would have picked freelancer — and it is why this is written as a backfill of
NULLs only rather than a blanket UPDATE: a tenant that has since chosen
freelancer is left alone in both directions.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None

#: No tables. Declared anyway because `tests/test_migration_parity.py` reads
#: this from every revision and unions them against 0001's exclusion list.
NEW_TABLES: tuple[str, ...] = ()

#: Must match `suliko.domain.plans.TenantPlan.BUREAU`. Spelled literally
#: rather than imported: a migration has to keep meaning what it meant on the
#: day it ran, and importing the enum would let a later rename rewrite history.
BUREAU = "bureau"


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE tenants
               SET plan = :plan
             WHERE plan IS NULL
            """
        ).bindparams(plan=BUREAU)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE tenants
               SET plan = NULL
             WHERE plan = :plan
            """
        ).bindparams(plan=BUREAU)
    )
