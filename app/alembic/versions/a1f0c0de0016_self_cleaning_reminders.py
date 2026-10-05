"""Self-cleaning reminders (spec §83).

Additive: one boolean on events and two nullable columns on deliveries, so a
code-only rollback is safe (the old code ignores them). The downgrade drops
them; messages already deleted in Discord stay deleted.

Revision ID: a1f0c0de0016
Revises: a1f0c0de0015
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a1f0c0de0016'
down_revision: Union[str, Sequence[str], None] = 'a1f0c0de0015'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    events = {c["name"] for c in inspector.get_columns("events")}
    deliveries = {c["name"] for c in inspector.get_columns("deliveries")}
    if "clean_up_reminders" not in events:
        op.add_column("events", sa.Column("clean_up_reminders", sa.Boolean(), nullable=False, server_default=sa.false()))
    if "message_deleted_at" not in deliveries:
        op.add_column("deliveries", sa.Column("message_deleted_at", sa.DateTime(timezone=True), nullable=True))
    if "cleanup_error" not in deliveries:
        op.add_column("deliveries", sa.Column("cleanup_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("deliveries", "cleanup_error")
    op.drop_column("deliveries", "message_deleted_at")
    op.drop_column("events", "clean_up_reminders")
