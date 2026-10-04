"""A message per reminder (spec §77).

Additive: one nullable column, so a code-only rollback is safe (the old code
ignores it). The downgrade drops the column, which loses the per-reminder
messages; reminders then use the event's message again.

Revision ID: a1f0c0de0012
Revises: a1f0c0de0011
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a1f0c0de0012'
down_revision: Union[str, Sequence[str], None] = 'a1f0c0de0011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("event_reminders")}
    if "message" not in columns:
        op.add_column("event_reminders", sa.Column("message", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("event_reminders", "message")
