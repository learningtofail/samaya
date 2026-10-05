"""Player registry and gift code redemption (spec §79, §80.3).

Additive: one nullable column and three new tables, so a code-only rollback is
safe. The downgrade drops them, which loses the roster and every redemption
result.

Revision ID: a1f0c0de0013
Revises: a1f0c0de0012
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a1f0c0de0013'
down_revision: Union[str, Sequence[str], None] = 'a1f0c0de0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    if "game_number" not in {c["name"] for c in inspector.get_columns("kingdoms")}:
        op.add_column("kingdoms", sa.Column("game_number", sa.Integer(), nullable=True))

    if "players" not in tables:
        op.create_table(
            "players",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("kingdom_id", sa.Integer(), sa.ForeignKey("kingdoms.id", ondelete="CASCADE"), nullable=False),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
            sa.Column("fid", sa.String(20), nullable=False),
            sa.Column("kid", sa.Integer(), nullable=True),
            sa.Column("name", sa.String(60), nullable=True),
            sa.Column("note", sa.String(40), nullable=True),
            sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("kingdom_id", "fid", name="uq_player_kingdom_fid"),
            sa.CheckConstraint("fid <> ''", name="ck_player_fid_set"),
        )
        op.create_index("ix_players_tenant", "players", ["tenant_id"])

    if "redemption_runs" not in tables:
        op.create_table(
            "redemption_runs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("kingdom_id", sa.Integer(), sa.ForeignKey("kingdoms.id", ondelete="CASCADE"), nullable=False),
            sa.Column("code", sa.Text(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False),
            sa.Column("stop_reason", sa.Text(), nullable=True),
            sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.CheckConstraint(
                "status IN ('queued', 'running', 'done', 'stopped', 'cancelled')", name="ck_redemption_run_status"),
        )

    if "redemption_results" not in tables:
        op.create_table(
            "redemption_results",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("run_id", sa.Integer(), sa.ForeignKey("redemption_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
            sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.id", ondelete="CASCADE"), nullable=True),
            sa.Column("fid", sa.String(20), nullable=False),
            sa.Column("kid", sa.Integer(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False),
            sa.Column("message", sa.Text(), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("cooldowns", sa.Integer(), nullable=False),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("run_id", "fid", name="uq_redemption_result_fid"),
        )
        op.create_index("ix_redemption_results_run_status", "redemption_results", ["run_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_redemption_results_run_status", table_name="redemption_results")
    op.drop_table("redemption_results")
    op.drop_table("redemption_runs")
    op.drop_index("ix_players_tenant", table_name="players")
    op.drop_table("players")
    op.drop_column("kingdoms", "game_number")
