"""One-time migration: adds `announcements.event_offset_minutes` and
creates the `announcement_templates` table (spec §27 — reusable,
placeholder-aware Announcement templates, e.g. "Daily Reset Warning" sent
15 minutes before a 00:00 UTC reset).

Run this ONCE against production, after taking a backup, BEFORE deploying
the new application code (the new models/db.py expects both to already
exist). Not wired into Alembic — see migrate_to_multitenant.py's own
docstring for why this repo hand-writes migrations instead.

Usage:
    cd app && python migrate_add_announcement_templates.py

Safe to re-run: checks whether the column/table already exists before
adding it (see _column_exists/_table_exists below), so an interrupted run
can just be re-run.

What it does:
  1. Adds announcements.event_offset_minutes INTEGER NOT NULL DEFAULT 0 —
     every existing announcement gets 0, matching its actual behavior
     before this field existed (its body_markdown has no {event_time}/
     {event_time_relative} placeholders to offset, since those didn't
     exist yet either).
  2. Creates announcement_templates (id, owning_tenant_id, name,
     title_template, body_template, leadership_only, event_offset_minutes,
     created_at) with a unique (owning_tenant_id, name) constraint. No
     rows are seeded — every tenant starts with zero templates, same as
     zero announcements before any were created.
"""
import asyncio
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://taraka:password@db:5432/kingshot_scheduler"
)


async def _table_exists(conn, table_name: str) -> bool:
    result = await conn.execute(
        text("SELECT 1 FROM information_schema.tables WHERE table_name = :t"),
        {"t": table_name},
    )
    return result.scalar() is not None


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
        print("Step 1: adding announcements.event_offset_minutes...")
        if not await _column_exists(conn, "announcements", "event_offset_minutes"):
            await conn.execute(text(
                "ALTER TABLE announcements ADD COLUMN event_offset_minutes INTEGER NOT NULL DEFAULT 0"
            ))
            print("  done")
        else:
            print("  already present, skipping")

        print("Step 2: creating announcement_templates...")
        if not await _table_exists(conn, "announcement_templates"):
            await conn.execute(text(
                """
                CREATE TABLE announcement_templates (
                    id                      SERIAL PRIMARY KEY,
                    owning_tenant_id        INTEGER NOT NULL REFERENCES tenants(id),
                    name                    TEXT NOT NULL,
                    title_template          TEXT NOT NULL,
                    body_template           TEXT NOT NULL,
                    leadership_only         BOOLEAN NOT NULL DEFAULT false,
                    event_offset_minutes    INTEGER NOT NULL DEFAULT 0,
                    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
                    CONSTRAINT uq_announcement_template_name UNIQUE (owning_tenant_id, name)
                )
                """
            ))
            print("  done")
        else:
            print("  already present, skipping")

    await engine.dispose()
    print("\nMigration complete. Every existing announcement now has")
    print("event_offset_minutes=0 (no behavior change), and every tenant")
    print("starts with zero announcement templates. Deploy the new")
    print("application code next.")


if __name__ == "__main__":
    asyncio.run(migrate())
