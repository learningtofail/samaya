"""Schedule board (spec §82).

Additive: six nullable columns on audience_destinations, so a code-only
rollback is safe (the old code ignores them). The downgrade drops them; any
board message already in Discord stays there until someone deletes it.

Revision ID: a1f0c0de0015
Revises: a1f0c0de0014
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a1f0c0de0015'
down_revision: Union[str, Sequence[str], None] = 'a1f0c0de0014'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "audience_destinations"
CHECK = "ck_audience_destination_board_scope"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns(TABLE)}
    if "board_scope" not in columns:
        op.add_column(TABLE, sa.Column("board_scope", sa.Text(), nullable=True))
    if "board_tenant_id" not in columns:
        op.add_column(TABLE, sa.Column("board_tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True))
    if "board_message_id" not in columns:
        op.add_column(TABLE, sa.Column("board_message_id", sa.Text(), nullable=True))
    if "board_hash" not in columns:
        op.add_column(TABLE, sa.Column("board_hash", sa.String(64), nullable=True))
    if "board_refreshed_at" not in columns:
        op.add_column(TABLE, sa.Column("board_refreshed_at", sa.DateTime(timezone=True), nullable=True))
    if "board_error" not in columns:
        op.add_column(TABLE, sa.Column("board_error", sa.Text(), nullable=True))
    if CHECK not in {c["name"] for c in inspector.get_check_constraints(TABLE)}:
        op.create_check_constraint(CHECK, TABLE, "board_scope IS NULL OR board_scope IN ('kingdom', 'alliance')")


def downgrade() -> None:
    op.drop_constraint(CHECK, TABLE, type_="check")
    for name in ("board_error", "board_refreshed_at", "board_hash", "board_message_id", "board_tenant_id", "board_scope"):
        op.drop_column(TABLE, name)
