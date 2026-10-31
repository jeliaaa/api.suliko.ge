"""Google Drive mirror of order files.

Revision ID: 0039
Revises: 0038
Create Date: 2026-10-31

* ``order_files.drive_file_id``: the Drive file this row was copied to, NULL
  while it has not been (or the organisation has no Drive connected).
* ``order_files.drive_error``: why the last copy failed, for the order page.

The order's Drive folder is ``orders.google_drive_folder``, which already
exists (the Tarjimnebi import fills it). No data is touched.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("order_files", sa.Column("drive_file_id", sa.String(100), nullable=True))
    op.add_column("order_files", sa.Column("drive_error", sa.String(300), nullable=True))


def downgrade() -> None:
    op.drop_column("order_files", "drive_error")
    op.drop_column("order_files", "drive_file_id")
