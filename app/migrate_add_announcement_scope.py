"""One-time migration: adds `announcements.scope` (spec §49 — Announcements
gain the same alliance/kingdom-wide scope concept EventDefinition already
has, so both use the same single "Owning Alliance" selector in the admin
UI instead of Events having one thing and Announcements another).

Run this ONCE against production, after taking a backup, BEFORE deploying
the new application code (the new models/db.py expects the column to
already exist). Not wired into Alembic — see migrate_to_multitenant.py's
own docstring for why this repo hand-writes migrations instead.

Usage:
    cd app && python migrate_add_announcement_scope.py

Safe to re-run: checks whether the column already exists before adding it,
so an interrupted run (or running this after it's already been applied)
is a no-op.

What it does:
  Adds announcements.scope TEXT NOT NULL DEFAULT 'alliance', plus the same
  ck_scope_valid-shaped CHECK constraint EventDefinition.scope already
  carries — every existing announcement becomes 'alliance', matching what
  it actually was before this field existed (an explicit multi-target
  list with no kingdom-wide concept at all). scope is a display/ownership
  label only — delivery still goes through the existing explicit
  AnnouncementTarget rows either way; picking kingdom-wide doesn't change
  how an announcement is actually sent.
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


async def _constraint_exists(conn, constraint_name: str) -> bool:
    result = await conn.execute(
        text("SELECT 1 FROM pg_constraint WHERE conname = :c"),
        {"c": constraint_name},
    )
    return result.scalar() is not None


async def migrate():
    engine = create_async_engine(DATABASE_URL, echo=False)

    async with engine.begin() as conn:
        print("Step 1: adding announcements.scope...")
        if not await _column_exists(conn, "announcements", "scope"):
            await conn.execute(text(
                "ALTER TABLE announcements ADD COLUMN scope TEXT NOT NULL DEFAULT 'alliance'"
            ))
            print("  done")
        else:
            print("  already exists, skipping")

        print("Step 2: adding ck_announcement_scope_valid...")
        if not await _constraint_exists(conn, "ck_announcement_scope_valid"):
            await conn.execute(text(
                "ALTER TABLE announcements ADD CONSTRAINT ck_announcement_scope_valid "
                "CHECK (scope IN ('alliance', 'kingdom-wide'))"
            ))
            print("  done")
        else:
            print("  already exists, skipping")

    await engine.dispose()
    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(migrate())
