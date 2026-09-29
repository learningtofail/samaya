"""One-time migration: adds `tenants.icon_image_data` and
`kingdoms.public_site_title`/`kingdoms.admin_console_title` (spec §38 —
uploadable alliance icons and editable kingdom-wide page titles).

Run this ONCE against production, after taking a backup, BEFORE deploying
the new application code (the new models/db.py expects these columns to
already exist). Not wired into Alembic — see migrate_to_multitenant.py's
own docstring for why this repo hand-writes migrations instead.

Usage:
    cd app && python migrate_add_tenant_icon_and_branding.py

Safe to re-run: checks whether each column already exists before adding
it, so an interrupted run (or running this after it's already been
applied) is a no-op.

What it does:
  Adds tenants.icon_image_data TEXT, nullable, no default — every existing
  tenant has no icon, matching its actual behavior before this field
  existed. Adds kingdoms.public_site_title TEXT and
  kingdoms.admin_console_title TEXT, both nullable, no default — NULL
  means "use the hardcoded default title," so an existing Kingdom row
  renders exactly as it always has until an admin sets one.
"""
import asyncio
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://taraka:password@db:5432/kingshot_scheduler"
)


async def _column_exists(conn, table_name: str, column_name: str) -> bool:
    result = await conn.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c"
        ),
        {"t": table_name, "c": column_name},
    )
    return result.scalar() is not None


async def _add_column_if_missing(conn, table: str, column: str, ddl_type: str):
    if not await _column_exists(conn, table, column):
        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
        print(f"  added {table}.{column}")
    else:
        print(f"  {table}.{column} already exists, skipping")


async def migrate():
    engine = create_async_engine(DATABASE_URL, echo=False)

    async with engine.begin() as conn:
        print("Step 1: adding tenants.icon_image_data...")
        await _add_column_if_missing(conn, "tenants", "icon_image_data", "TEXT")

        print("Step 2: adding kingdoms branding columns...")
        await _add_column_if_missing(conn, "kingdoms", "public_site_title", "TEXT")
        await _add_column_if_missing(conn, "kingdoms", "admin_console_title", "TEXT")

    await engine.dispose()
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(migrate())
