"""Kingdom interface languages (spec §72.2).

Additive: two nullable columns, so a code-only rollback is safe and the
downgrade is a plain column drop. NULL `default_locale` means English and NULL
`enabled_locales` means English only, which is exactly how the site behaves
before this revision.

Revision ID: a1f0c0de0007
Revises: a1f0c0de0006
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a1f0c0de0007'
down_revision: Union[str, Sequence[str], None] = 'a1f0c0de0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("kingdoms")}
    if "default_locale" not in cols:
        op.add_column("kingdoms", sa.Column("default_locale", sa.Text(), nullable=True))
    if "enabled_locales" not in cols:
        op.add_column("kingdoms", sa.Column("enabled_locales", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("kingdoms", "enabled_locales")
    op.drop_column("kingdoms", "default_locale")
