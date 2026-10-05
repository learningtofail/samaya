"""Pause failing destinations (spec §81).

Additive: three columns on audience_destinations, so a code-only rollback is
safe (the old code ignores them). The downgrade drops them, which un-pauses
every destination.

Revision ID: a1f0c0de0014
Revises: a1f0c0de0013
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a1f0c0de0014'
down_revision: Union[str, Sequence[str], None] = 'a1f0c0de0013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("audience_destinations")}
    if "consecutive_failures" not in columns:
        op.add_column("audience_destinations", sa.Column(
            "consecutive_failures", sa.Integer(), nullable=False, server_default="0"))
    if "paused_at" not in columns:
        op.add_column("audience_destinations", sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True))
    if "pause_reason" not in columns:
        op.add_column("audience_destinations", sa.Column("pause_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("audience_destinations", "pause_reason")
    op.drop_column("audience_destinations", "paused_at")
    op.drop_column("audience_destinations", "consecutive_failures")
