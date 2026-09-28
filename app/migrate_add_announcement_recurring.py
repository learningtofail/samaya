"""One-time migration: adds leadership_only/recurring/interval_days to the
existing `announcements` table (spec §13 enhancements — recurring
announcements + a display/categorization leadership flag).

Run this ONCE against production, after taking a backup, BEFORE deploying
the new application code (the new models/db.py expects these columns to
already exist). Not wired into Alembic — see migrate_to_multitenant.py's
own docstring for why this repo hand-writes migrations instead; the same
gap applies here and isn't fixed by this script either.

Usage:
    cd app && python migrate_add_announcement_recurring.py

Safe to re-run: checks whether each column already exists before adding it
(see _column_exists below), so an interrupted run can just be re-run.

What it does:
  1. Adds leadership_only BOOLEAN NOT NULL DEFAULT false — every existing
     announcement row is treated as a general (non-leadership) announcement,
     since the distinction didn't exist before this and there's no reliable
     way to infer it retroactively.
  2. Adds recurring BOOLEAN NOT NULL DEFAULT false and interval_days
     INTEGER (nullable) — every existing row becomes a one-time
     announcement, matching its actual behavior before this migration (the
     re-arm logic in scheduler/announcements.py did not exist yet).
  3. Adds the same check constraint the ORM model declares
     (ck_announcement_interval_positive_if_recurring): interval_days must
     be a positive integer when recurring is true, and is otherwise
     unconstrained (expected to be NULL, though the constraint doesn't
     enforce that — mirrors the model's own model_validator, which is
     where NULLing it on non-recurring writes actually happens).
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
        text("SELECT 1 FROM pg_constraint WHERE conname = :name"),
        {"name": constraint_name},
    )
    return result.scalar() is not None


async def migrate():
    engine = create_async_engine(DATABASE_URL, echo=False)

    async with engine.begin() as conn:
        print("Step 1: adding leadership_only...")
        if not await _column_exists(conn, "announcements", "leadership_only"):
            await conn.execute(text(
                "ALTER TABLE announcements ADD COLUMN leadership_only BOOLEAN NOT NULL DEFAULT false"
            ))
            print("  done")
        else:
            print("  already present, skipping")

        print("Step 2: adding recurring + interval_days...")
        if not await _column_exists(conn, "announcements", "recurring"):
            await conn.execute(text(
                "ALTER TABLE announcements ADD COLUMN recurring BOOLEAN NOT NULL DEFAULT false"
            ))
            await conn.execute(text(
                "ALTER TABLE announcements ADD COLUMN interval_days INTEGER"
            ))
            print("  done")
        else:
            print("  already present, skipping")

        print("Step 3: adding ck_announcement_interval_positive_if_recurring...")
        if not await _constraint_exists(conn, "ck_announcement_interval_positive_if_recurring"):
            await conn.execute(text(
                "ALTER TABLE announcements ADD CONSTRAINT "
                "ck_announcement_interval_positive_if_recurring "
                "CHECK (NOT recurring OR interval_days > 0)"
            ))
            print("  done")
        else:
            print("  already present, skipping")

    await engine.dispose()
    print("\nMigration complete. All existing announcements are now")
    print("leadership_only=false, recurring=false, interval_days=NULL —")
    print("unchanged one-time/general behavior. Deploy the new application")
    print("code next.")


if __name__ == "__main__":
    asyncio.run(migrate())
