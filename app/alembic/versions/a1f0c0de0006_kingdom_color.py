"""Kingdom brand color (spec §63).

Additive: one nullable column, so a code-only rollback is safe and the
downgrade is a plain column drop. NULL keeps today's built-in Kingdom gold.

Revision ID: a1f0c0de0006
Revises: a1f0c0de0005
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a1f0c0de0006'
down_revision: Union[str, Sequence[str], None] = 'a1f0c0de0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("kingdoms")}
    if "color" not in cols:
        op.add_column("kingdoms", sa.Column("color", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("kingdoms", "color")
