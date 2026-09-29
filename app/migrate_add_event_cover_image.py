"""One-time migration: adds `event_definitions.cover_image_data` (spec
§33 — Discord Scheduled Event cover images/banners).

Run this ONCE against production, after taking a backup, BEFORE deploying
the new application code (the new models/db.py expects the column to
already exist). Not wired into Alembic — see migrate_to_multitenant.py's
own docstring for why this repo hand-writes migrations instead.

Usage:
    cd app && python migrate_add_event_cover_image.py

Safe to re-run: checks whether the column already exists before adding it,
so an interrupted run (or running this after it's already been applied)
is a no-op.

What it does:
  Adds event_definitions.cover_image_data TEXT, nullable, no default —
  every existing event has no cover image, matching its actual behavior
  before this field existed (Discord Scheduled Events created by this app
  have never set an image, since the field didn't exist to set one from).
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


async def migrate():
    engine = create_async_engine(DATABASE_URL, echo=False)

    async with engine.begin() as conn:
        print("Step 1: adding event_definitions.cover_image_data...")
        if not await _column_exists(conn, "event_definitions", "cover_image_data"):
            await conn.execute(text(
                "ALTER TABLE event_definitions ADD COLUMN cover_image_data TEXT"
            ))
            print("  done")
        else:
            print("  already exists, skipping")

    await engine.dispose()
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(migrate())
