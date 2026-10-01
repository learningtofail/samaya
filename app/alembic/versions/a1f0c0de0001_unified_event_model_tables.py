"""unified event model tables (spec §66, Phase 1)

Revision ID: a1f0c0de0001
Revises: 6fc935931248
Create Date: 2026-10-01 10:58:44.037574

Additive: creates the new event_types / events / event_alliances /
event_reminders / event_occurrences / deliveries tables, adds
tenants.notification_channel_id/notification_role_id and users.display_name,
and seeds a "General" event type per existing Kingdom. Nothing is dropped
here; the old tables go in the closure-phase revision.

It also reconciles production with the baseline (spec §66.8). Production was
built by the hand-written migrate_*.py scripts, so `alembic check` reported
drift against the models: unique constraints carrying Postgres default names
instead of the models' uq_* names, and two created_at columns that production
has NOT NULL but the models used to call nullable. Every statement below is a
no-op on a database built from the baseline revision, so one revision works
for both.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a1f0c0de0001'
down_revision: Union[str, Sequence[str], None] = '6fc935931248'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    #
    op.create_table('event_types',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kingdom_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('color', sa.Text(), nullable=False),
    sa.Column('default_duration_hours', sa.Numeric(precision=4, scale=1), nullable=True),
    sa.Column('default_interval_days', sa.Integer(), nullable=True),
    sa.Column('default_message', sa.Text(), nullable=False),
    sa.Column('default_reminder_minutes', sa.JSON(), nullable=False),
    sa.Column('default_mention_role', sa.Boolean(), nullable=False),
    sa.Column('sort_order', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.CheckConstraint('default_duration_hours IS NULL OR default_duration_hours > 0', name='ck_event_type_duration_positive'),
    sa.CheckConstraint('default_interval_days IS NULL OR default_interval_days > 0', name='ck_event_type_interval_positive'),
    sa.ForeignKeyConstraint(['kingdom_id'], ['kingdoms.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('kingdom_id', 'name', name='uq_event_type_name')
    )
    op.create_table('events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('owning_tenant_id', sa.Integer(), nullable=False),
    sa.Column('type_id', sa.Integer(), nullable=False),
    sa.Column('series_id', sa.Text(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('scope', sa.Text(), nullable=False),
    sa.Column('leadership_only', sa.Boolean(), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('location', sa.Text(), nullable=False),
    sa.Column('start_time_utc', sa.Time(), nullable=False),
    sa.Column('duration_hours', sa.Numeric(precision=4, scale=1), nullable=True),
    sa.Column('recurrence_kind', sa.Text(), nullable=False),
    sa.Column('interval_days', sa.Integer(), nullable=True),
    sa.Column('anchor_date', sa.Date(), nullable=False),
    sa.Column('until_date', sa.Date(), nullable=True),
    sa.Column('mention_role', sa.Boolean(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('cover_image_data', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.CheckConstraint("(recurrence_kind = 'none' AND interval_days IS NULL) OR (recurrence_kind = 'interval_days' AND interval_days IS NOT NULL AND interval_days > 0)", name='ck_event_recurrence_consistent'),
    sa.CheckConstraint("recurrence_kind IN ('none', 'interval_days')", name='ck_event_recurrence_kind'),
    sa.CheckConstraint("scope IN ('alliance', 'kingdom-wide')", name='ck_event_scope_valid'),
    sa.CheckConstraint('duration_hours IS NULL OR duration_hours > 0', name='ck_event_duration_positive'),
    sa.CheckConstraint('until_date IS NULL OR until_date >= anchor_date', name='ck_event_until_after_anchor'),
    sa.ForeignKeyConstraint(['owning_tenant_id'], ['tenants.id'], ),
    sa.ForeignKeyConstraint(['type_id'], ['event_types.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('event_alliances',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('event_id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('message_override', sa.Text(), nullable=True),
    sa.Column('notification_channel_id', sa.Text(), nullable=True),
    sa.Column('notification_role_id', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['event_id'], ['events.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('event_id', 'tenant_id', name='uq_event_alliance')
    )
    op.create_table('event_occurrences',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('event_id', sa.Integer(), nullable=False),
    sa.Column('occurrence_date', sa.Date(), nullable=False),
    sa.Column('start_datetime_utc', sa.DateTime(timezone=True), nullable=False),
    sa.Column('end_datetime_utc', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('start_datetime_utc_override', sa.DateTime(timezone=True), nullable=True),
    sa.Column('message_override', sa.Text(), nullable=True),
    sa.Column('generated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.CheckConstraint("status IN ('scheduled', 'cancelled')", name='ck_event_occurrence_status'),
    sa.ForeignKeyConstraint(['event_id'], ['events.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('event_id', 'occurrence_date', name='uq_event_occurrence')
    )
    op.create_table('event_reminders',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('event_id', sa.Integer(), nullable=False),
    sa.Column('minutes_before', sa.Integer(), nullable=False),
    sa.CheckConstraint('minutes_before >= 0', name='ck_event_reminder_minutes'),
    sa.ForeignKeyConstraint(['event_id'], ['events.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('event_id', 'minutes_before', name='uq_event_reminder')
    )
    op.create_table('deliveries',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('occurrence_id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('reminder_minutes', sa.Integer(), nullable=False),
    sa.Column('due_at_utc', sa.DateTime(timezone=True), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('claimed_at_utc', sa.DateTime(timezone=True), nullable=True),
    sa.Column('discord_event_id', sa.Text(), nullable=True),
    sa.Column('discord_message_id', sa.Text(), nullable=True),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.Column('posted_at_utc', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("(kind = 'discord_event' AND reminder_minutes = -1) OR (kind = 'reminder' AND reminder_minutes >= 0)", name='ck_delivery_reminder_minutes'),
    sa.CheckConstraint("kind IN ('discord_event', 'reminder')", name='ck_delivery_kind'),
    sa.CheckConstraint("status IN ('pending', 'sending', 'posted', 'error', 'cancelled')", name='ck_delivery_status'),
    sa.ForeignKeyConstraint(['occurrence_id'], ['event_occurrences.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('occurrence_id', 'tenant_id', 'kind', 'reminder_minutes', name='uq_delivery')
    )
    op.create_index('ix_deliveries_status_due', 'deliveries', ['status', 'due_at_utc'], unique=False)

    # --- production drift (see module docstring) -------------------------
    for old_name, new_name, table in (
        ("announcement_targets_announcement_id_tenant_id_key", "uq_announcement_target", "announcement_targets"),
        ("event_tenant_notifications_event_id_tenant_id_key", "uq_event_tenant_notification", "event_tenant_notifications"),
        ("user_kingdoms_user_id_kingdom_id_key", "uq_user_kingdom", "user_kingdoms"),
        ("user_tenants_user_id_tenant_id_key", "uq_user_tenant", "user_tenants"),
    ):
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = '{old_name}' AND conrelid = '{table}'::regclass
                ) THEN
                    ALTER TABLE {table} RENAME CONSTRAINT {old_name} TO {new_name};
                END IF;
            END $$;
            """
        )

    for table in ("announcement_templates", "discord_servers"):
        op.execute(f"UPDATE {table} SET created_at = now() WHERE created_at IS NULL")
        op.alter_column(
            table, "created_at",
            existing_type=postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            existing_server_default=sa.text("now()"),
        )

    op.add_column('tenants', sa.Column('notification_channel_id', sa.Text(), server_default='', nullable=False))
    op.add_column('tenants', sa.Column('notification_role_id', sa.Text(), server_default='', nullable=False))
    op.add_column('users', sa.Column('display_name', sa.Text(), nullable=True))

    # A default type so a Kingdom can create events before defining its own.
    op.execute(
        """
        INSERT INTO event_types (kingdom_id, name, color, default_message, default_reminder_minutes,
                                 default_mention_role, sort_order)
        SELECT id, 'General', '#475569', '', '[]'::json, false, 0 FROM kingdoms
        ON CONFLICT ON CONSTRAINT uq_event_type_name DO NOTHING
        """
    )


def downgrade() -> None:
    """Reverses the additive parts. The production-drift fixes (constraint
    names, NOT NULL on created_at) are deliberately left in place: they make
    the schema match the models and reverting them would reintroduce drift."""
    op.drop_column('users', 'display_name')
    op.drop_column('tenants', 'notification_role_id')
    op.drop_column('tenants', 'notification_channel_id')
    op.drop_index('ix_deliveries_status_due', table_name='deliveries')
    op.drop_table('deliveries')
    op.drop_table('event_reminders')
    op.drop_table('event_occurrences')
    op.drop_table('event_alliances')
    op.drop_table('events')
    op.drop_table('event_types')
