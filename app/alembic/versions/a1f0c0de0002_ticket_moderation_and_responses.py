"""ticket moderation and public responses (spec §66.10, §66.11)

Revision ID: a1f0c0de0002
Revises: a1f0c0de0001
Create Date: 2026-10-01 12:30:00.000000

Tickets gain the `dismissed` status and a `ticket_responses` table. An error
report now references an event occurrence only, so the old announcement
reference goes and the occurrence reference moves to `event_occurrences`.
Existing references are cleared: they pointed at old-model rows that the
closure revision drops, and no ticket data has to survive (§66 decisions).

The foreign keys are dropped by what they reference, not by name, because
production's constraint names came from hand-written scripts.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a1f0c0de0002'
down_revision: Union[str, Sequence[str], None] = 'a1f0c0de0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE tickets DROP CONSTRAINT IF EXISTS ck_ticket_related_mutually_exclusive")
    op.execute(
        """
        DO $$
        DECLARE r record;
        BEGIN
            FOR r IN SELECT conname FROM pg_constraint
                     WHERE conrelid = 'tickets'::regclass AND contype = 'f'
                       AND confrelid IN ('occurrences'::regclass, 'announcements'::regclass)
            LOOP
                EXECUTE format('ALTER TABLE tickets DROP CONSTRAINT %I', r.conname);
            END LOOP;
        END $$;
        """
    )
    op.execute("UPDATE tickets SET related_occurrence_id = NULL")
    op.drop_column('tickets', 'related_announcement_id')
    op.create_foreign_key(
        'fk_ticket_related_occurrence', 'tickets', 'event_occurrences',
        ['related_occurrence_id'], ['id'], ondelete='SET NULL',
    )
    op.execute("ALTER TABLE tickets DROP CONSTRAINT IF EXISTS ck_ticket_status")
    op.create_check_constraint(
        'ck_ticket_status', 'tickets',
        "status IN ('open', 'planned', 'in_progress', 'done', 'declined', 'dismissed')",
    )
    op.create_table(
        'ticket_responses',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ticket_id', sa.Integer(), nullable=False),
        sa.Column('author_user_id', sa.Integer(), nullable=True),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.CheckConstraint('length(body) > 0 AND length(body) <= 2000', name='ck_ticket_response_length'),
        sa.ForeignKeyConstraint(['author_user_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['ticket_id'], ['tickets.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    """Restores the old columns and constraints but not the data: cleared
    occurrence references and responses are gone."""
    op.drop_table('ticket_responses')
    op.execute("UPDATE tickets SET status = 'declined' WHERE status = 'dismissed'")
    op.drop_constraint('ck_ticket_status', 'tickets', type_='check')
    op.create_check_constraint(
        'ck_ticket_status', 'tickets', "status IN ('open', 'planned', 'in_progress', 'done', 'declined')",
    )
    op.drop_constraint('fk_ticket_related_occurrence', 'tickets', type_='foreignkey')
    op.add_column('tickets', sa.Column('related_announcement_id', sa.Integer(), nullable=True))
    op.create_foreign_key(None, 'tickets', 'occurrences', ['related_occurrence_id'], ['id'])
    op.create_foreign_key(None, 'tickets', 'announcements', ['related_announcement_id'], ['id'])
    op.create_check_constraint(
        'ck_ticket_related_mutually_exclusive', 'tickets',
        'related_occurrence_id IS NULL OR related_announcement_id IS NULL',
    )
