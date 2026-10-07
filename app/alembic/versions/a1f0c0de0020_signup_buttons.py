"""Notify me and I'm in buttons (spec §87).

Additive: four tables, four columns on events and four on deliveries, so a
code-only rollback is safe (the old code ignores them). The event columns
default to false, so no existing event gets a button until a leader turns it
on; new events default to true in code. The downgrade drops everything, which
loses the attendance groups, role mappings, subscriptions and RSVPs. Roles
already created in Discord stay.

The id follows 0019 (time polls); 0017 and 0018 stay reserved for the PWA
(spec §78).

Revision ID: a1f0c0de0020
Revises: a1f0c0de0019
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a1f0c0de0020'
down_revision: Union[str, Sequence[str], None] = 'a1f0c0de0019'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    events = {c["name"] for c in inspector.get_columns("events")}
    deliveries = {c["name"] for c in inspector.get_columns("deliveries")}

    if "signup_groups" not in tables:
        op.create_table(
            "signup_groups",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("kingdom_id", sa.Integer(), sa.ForeignKey("kingdoms.id", ondelete="CASCADE"), nullable=False),
            sa.Column("name", sa.String(80), nullable=False),
            sa.Column("exclusive_roles", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("attendance_window", sa.String(8), nullable=False, server_default="none"),
            sa.UniqueConstraint("kingdom_id", "name", name="uq_signup_group_name"),
            sa.CheckConstraint("attendance_window IN ('none', 'day', 'week')", name="ck_signup_group_window"),
        )

    for name in ("signup_enabled", "signup_mention", "rsvp_enabled"):
        if name not in events:
            op.add_column("events", sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false()))
    if "signup_group_id" not in events:
        op.add_column("events", sa.Column(
            "signup_group_id", sa.Integer(), sa.ForeignKey("signup_groups.id", ondelete="SET NULL"), nullable=True))

    if "rsvp_count_shown" not in deliveries:
        op.add_column("deliveries", sa.Column("rsvp_count_shown", sa.Integer(), nullable=True))
    if "rsvp_base_content" not in deliveries:
        op.add_column("deliveries", sa.Column("rsvp_base_content", sa.Text(), nullable=True))
    if "rsvp_edited_at" not in deliveries:
        op.add_column("deliveries", sa.Column("rsvp_edited_at", sa.DateTime(timezone=True), nullable=True))
    if "rsvp_edit_error_at" not in deliveries:
        op.add_column("deliveries", sa.Column("rsvp_edit_error_at", sa.DateTime(timezone=True), nullable=True))

    if "signup_roles" not in tables:
        op.create_table(
            "signup_roles",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=True),
            sa.Column("type_id", sa.Integer(), sa.ForeignKey("event_types.id", ondelete="CASCADE"), nullable=True),
            sa.Column("server_id", sa.Integer(), sa.ForeignKey("discord_servers.id", ondelete="CASCADE"), nullable=False),
            sa.Column("role_id", sa.Text(), nullable=False),
            sa.Column("role_name", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_by_samaya", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
            sa.CheckConstraint(
                "(event_id IS NOT NULL AND type_id IS NULL) OR (event_id IS NULL AND type_id IS NOT NULL)",
                name="ck_signup_role_one_scope",
            ),
            sa.CheckConstraint("role_id <> ''", name="ck_signup_role_id_set"),
        )
        op.create_index("uq_signup_role_event", "signup_roles", ["event_id", "server_id"], unique=True,
                        postgresql_where=sa.text("event_id IS NOT NULL"))
        op.create_index("uq_signup_role_type", "signup_roles", ["type_id", "server_id"], unique=True,
                        postgresql_where=sa.text("type_id IS NOT NULL"))

    if "event_subscriptions" not in tables:
        op.create_table(
            "event_subscriptions",
            sa.Column("server_id", sa.Integer(), sa.ForeignKey("discord_servers.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("role_id", sa.Text(), primary_key=True),
            sa.Column("voter_hash", sa.String(64), primary_key=True),
            sa.Column("subscribed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )

    if "occurrence_rsvps" not in tables:
        op.create_table(
            "occurrence_rsvps",
            sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("occurrence_date", sa.Date(), primary_key=True),
            sa.Column("voter_hash", sa.String(64), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_occurrence_rsvps_voter", "occurrence_rsvps", ["voter_hash", "occurrence_date"])


def downgrade() -> None:
    op.drop_table("occurrence_rsvps")
    op.drop_table("event_subscriptions")
    op.drop_table("signup_roles")
    op.drop_column("deliveries", "rsvp_edit_error_at")
    op.drop_column("deliveries", "rsvp_edited_at")
    op.drop_column("deliveries", "rsvp_base_content")
    op.drop_column("deliveries", "rsvp_count_shown")
    op.drop_column("events", "signup_group_id")
    op.drop_column("events", "rsvp_enabled")
    op.drop_column("events", "signup_mention")
    op.drop_column("events", "signup_enabled")
    op.drop_table("signup_groups")
