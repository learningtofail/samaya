"""Time polls (spec §84).

Additive: four new tables, so a code-only rollback is safe. The downgrade drops
them, which loses every poll, tally and posted-message record. Polls and votes
are kept 90 days anyway.

The id is 0019 because 0017 and 0018 are reserved by the PWA design (spec §78).
This revision therefore chains from 0016; whichever of those lands first must
re-point its own `down_revision`.

Revision ID: a1f0c0de0019
Revises: a1f0c0de0016
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a1f0c0de0019'
down_revision: Union[str, Sequence[str], None] = 'a1f0c0de0016'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "time_polls" not in tables:
        op.create_table(
            "time_polls",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("kingdom_id", sa.Integer(), sa.ForeignKey("kingdoms.id", ondelete="CASCADE"), nullable=False),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True),
            sa.Column("title", sa.String(80), nullable=False),
            sa.Column("occurrence_id", sa.Integer(), sa.ForeignKey("event_occurrences.id", ondelete="SET NULL"), nullable=True),
            sa.Column("status", sa.Text(), nullable=False),
            sa.Column("closes_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("winner_slot_id", sa.Integer(), nullable=True),
            sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.CheckConstraint("status IN ('open', 'closed', 'cancelled')", name="ck_time_poll_status"),
        )
        op.create_index("ix_time_polls_status_closes", "time_polls", ["status", "closes_at"])

    if "time_poll_slots" not in tables:
        op.create_table(
            "time_poll_slots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("poll_id", sa.Integer(), sa.ForeignKey("time_polls.id", ondelete="CASCADE"), nullable=False),
            sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.UniqueConstraint("poll_id", "starts_at", name="uq_time_poll_slot"),
        )
        op.create_foreign_key("fk_time_polls_winner_slot", "time_polls", "time_poll_slots",
                              ["winner_slot_id"], ["id"], ondelete="SET NULL")

    if "time_poll_votes" not in tables:
        op.create_table(
            "time_poll_votes",
            sa.Column("slot_id", sa.Integer(), sa.ForeignKey("time_poll_slots.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("voter_hash", sa.String(64), primary_key=True),
        )

    if "time_poll_messages" not in tables:
        op.create_table(
            "time_poll_messages",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("poll_id", sa.Integer(), sa.ForeignKey("time_polls.id", ondelete="CASCADE"), nullable=False),
            sa.Column("destination_id", sa.Integer(), sa.ForeignKey("audience_destinations.id", ondelete="SET NULL"), nullable=True),
            sa.Column("guild_id", sa.Text(), nullable=False),
            sa.Column("channel_id", sa.Text(), nullable=False),
            sa.Column("message_id", sa.Text(), nullable=False),
            sa.Column("content_hash", sa.String(64), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("poll_id", "channel_id", name="uq_time_poll_message_channel"),
        )


def downgrade() -> None:
    op.drop_table("time_poll_messages")
    op.drop_table("time_poll_votes")
    op.drop_constraint("fk_time_polls_winner_slot", "time_polls", type_="foreignkey")
    op.drop_table("time_poll_slots")
    op.drop_table("time_polls")
