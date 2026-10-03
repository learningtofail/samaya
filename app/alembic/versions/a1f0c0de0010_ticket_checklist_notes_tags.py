"""Ticket checklists, internal notes and tags (spec §76.5).

Additive: three new tables and one nullable column, so a code-only rollback is
safe. The downgrade drops them, which loses any checklist, note or tag data.

Revision ID: a1f0c0de0010
Revises: a1f0c0de0009
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a1f0c0de0010'
down_revision: Union[str, Sequence[str], None] = 'a1f0c0de0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    if "internal_notes" not in {c["name"] for c in inspector.get_columns("tickets")}:
        op.add_column("tickets", sa.Column("internal_notes", sa.Text(), nullable=True))

    if "ticket_checklist_items" not in tables:
        op.create_table(
            "ticket_checklist_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("ticket_id", sa.Integer(), sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("done", sa.Boolean(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.CheckConstraint("length(body) > 0 AND length(body) <= 200", name="ck_ticket_checklist_length"),
        )
        op.create_index("ix_ticket_checklist_items_ticket_id", "ticket_checklist_items", ["ticket_id"])

    if "ticket_tags" not in tables:
        op.create_table(
            "ticket_tags",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("color", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("name", name="uq_ticket_tag_name"),
            sa.CheckConstraint("length(name) > 0 AND length(name) <= 24", name="ck_ticket_tag_name_length"),
        )

    if "ticket_tag_links" not in tables:
        op.create_table(
            "ticket_tag_links",
            sa.Column("ticket_id", sa.Integer(), sa.ForeignKey("tickets.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("tag_id", sa.Integer(), sa.ForeignKey("ticket_tags.id", ondelete="CASCADE"), primary_key=True),
        )


def downgrade() -> None:
    op.drop_table("ticket_tag_links")
    op.drop_table("ticket_tags")
    op.drop_index("ix_ticket_checklist_items_ticket_id", table_name="ticket_checklist_items")
    op.drop_table("ticket_checklist_items")
    op.drop_column("tickets", "internal_notes")
