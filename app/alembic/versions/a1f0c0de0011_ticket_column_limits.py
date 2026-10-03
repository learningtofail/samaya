"""Ticket board WIP limits (spec §76.6).

Additive: one new table, so a code-only rollback is safe. The downgrade drops
it, which loses the configured limits.

Revision ID: a1f0c0de0011
Revises: a1f0c0de0010
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a1f0c0de0011'
down_revision: Union[str, Sequence[str], None] = 'a1f0c0de0010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if "ticket_column_limits" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "ticket_column_limits",
        sa.Column("status", sa.Text(), primary_key=True),
        sa.Column("wip_limit", sa.Integer(), nullable=False),
        sa.CheckConstraint("wip_limit >= 1 AND wip_limit <= 999", name="ck_ticket_column_limit_range"),
    )


def downgrade() -> None:
    op.drop_table("ticket_column_limits")
