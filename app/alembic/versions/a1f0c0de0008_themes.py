"""Public page themes (spec §71.9).

Additive: three new tables and one nullable column, so a code-only rollback is
safe (old code never reads them) and the downgrade is a plain drop.

Revision ID: a1f0c0de0008
Revises: a1f0c0de0007
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a1f0c0de0008'
down_revision: Union[str, Sequence[str], None] = 'a1f0c0de0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "theme_assets" not in tables:
        op.create_table(
            "theme_assets",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("kingdom_id", sa.Integer(), sa.ForeignKey("kingdoms.id"), nullable=False),
            sa.Column("sha256", sa.Text(), nullable=False),
            sa.Column("content", sa.LargeBinary(), nullable=False),
            sa.Column("width", sa.Integer(), nullable=False),
            sa.Column("height", sa.Integer(), nullable=False),
            sa.Column("credit", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("kingdom_id", "sha256", name="uq_theme_asset_hash"),
        )
    if "themes" not in tables:
        op.create_table(
            "themes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("kingdom_id", sa.Integer(), sa.ForeignKey("kingdoms.id"), nullable=False),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("bg", sa.Text(), nullable=False),
            sa.Column("accent", sa.Text(), nullable=False),
            sa.Column("accent_text", sa.Text(), nullable=False),
            sa.Column("primary", sa.Text(), nullable=False),
            sa.Column("font_heading", sa.Text(), nullable=True),
            sa.Column("font_body", sa.Text(), nullable=True),
            sa.Column("font_numerals", sa.Text(), nullable=True),
            sa.Column("banner_asset_id", sa.Integer(), sa.ForeignKey("theme_assets.id", ondelete="RESTRICT"), nullable=True),
            sa.Column("banner_overlay", sa.Numeric(3, 2), nullable=False, server_default=sa.text("0.96")),
            sa.Column("header_asset_id", sa.Integer(), sa.ForeignKey("theme_assets.id", ondelete="RESTRICT", name="fk_theme_header_asset"), nullable=True),
            sa.Column("header_mobile_asset_id", sa.Integer(), sa.ForeignKey("theme_assets.id", ondelete="RESTRICT", name="fk_theme_header_mobile_asset"), nullable=True),
            sa.Column("hero_wash", sa.Text(), nullable=False, server_default="#FFFFFF"),
            sa.Column("tint", sa.Text(), nullable=True),
            sa.Column("radius", sa.Integer(), nullable=True),
            sa.Column("art_height", sa.Integer(), nullable=True),
            sa.Column("copy", sa.JSON(), nullable=True),
            sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("kingdom_id", "name", name="uq_theme_name"),
            sa.CheckConstraint("banner_overlay >= 0.90 AND banner_overlay <= 1.00", name="ck_theme_overlay"),
            sa.CheckConstraint("radius IS NULL OR (radius >= 4 AND radius <= 14)", name="ck_theme_radius"),
            sa.CheckConstraint("art_height IS NULL OR (art_height >= 240 AND art_height <= 360)", name="ck_theme_art_height"),
        )
    if "scheduled_themes" not in tables:
        op.create_table(
            "scheduled_themes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("kingdom_id", sa.Integer(), sa.ForeignKey("kingdoms.id"), nullable=False),
            sa.Column("theme_id", sa.Integer(), sa.ForeignKey("themes.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("start_utc", sa.DateTime(timezone=True), nullable=False),
            sa.Column("end_utc", sa.DateTime(timezone=True), nullable=False),
            sa.Column("priority_level", sa.Integer(), nullable=False, server_default=sa.text("50")),
            sa.CheckConstraint("start_utc < end_utc", name="ck_scheduled_theme_window"),
            sa.CheckConstraint("priority_level >= 0 AND priority_level <= 1000", name="ck_scheduled_theme_priority"),
        )
        op.create_index("ix_scheduled_themes_window", "scheduled_themes", ["kingdom_id", "start_utc", "end_utc"])

    cols = {c["name"] for c in sa.inspect(bind).get_columns("kingdoms")}
    if "default_theme_id" not in cols:
        # batch mode: SQLite cannot add a constraint to an existing table.
        with op.batch_alter_table("kingdoms") as batch:
            batch.add_column(sa.Column("default_theme_id", sa.Integer(), nullable=True))
            batch.create_foreign_key("fk_kingdom_default_theme", "themes", ["default_theme_id"], ["id"], ondelete="RESTRICT")


def downgrade() -> None:
    with op.batch_alter_table("kingdoms") as batch:
        batch.drop_constraint("fk_kingdom_default_theme", type_="foreignkey")
        batch.drop_column("default_theme_id")
    op.drop_index("ix_scheduled_themes_window", table_name="scheduled_themes")
    op.drop_table("scheduled_themes")
    op.drop_table("themes")
    op.drop_table("theme_assets")
