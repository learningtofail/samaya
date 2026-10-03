"""Ticket board order (spec §76).

Additive: one nullable column, so a code-only rollback is safe and the
downgrade is a plain column drop. NULL means "never placed by hand".

Revision ID: a1f0c0de0009
Revises: a1f0c0de0008
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a1f0c0de0009'
down_revision: Union[str, Sequence[str], None] = 'a1f0c0de0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("tickets")}
    if "position" not in cols:
        op.add_column("tickets", sa.Column("position", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("tickets", "position")
