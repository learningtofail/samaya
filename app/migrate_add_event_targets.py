"""One-time migration: creates the `event_targets` table (spec §20 —
explicit extra (tenant, notification channel, notification role)
destinations for an event, independent of `scope`'s automatic same-Kingdom
fan-out).

Run this ONCE against production, after taking a backup, BEFORE deploying
the new application code (the new models/db.py expects this table to
already exist). Not wired into Alembic — see migrate_to_multitenant.py's
own docstring for why this repo hand-writes migrations instead.

Usage:
    cd app && python migrate_add_event_targets.py

Safe to re-run: checks whether the table already exists before creating it
(see _table_exists below), so an interrupted run can just be re-run.

What it does:
  Creates `event_targets` with columns id, event_id (FK -> event_definitions,
  ON DELETE CASCADE, matching the ORM's cascade="all, delete-orphan"),
  tenant_id (FK -> tenants), notification_channel_id, notification_role_id
  (both TEXT NOT NULL DEFAULT ''), and a unique constraint on
  (event_id, tenant_id) — one row per (event, tenant) pair, mirroring
  event_tenant_notifications' existing uq_event_tenant_notification.
  No existing data is touched; every existing event simply has zero rows
  here, meaning no explicit extra targets, which is exactly its current
  (pre-migration) behavior.
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


async def migrate():
    engine = create_async_engine(DATABASE_URL, echo=False)

    async with engine.begin() as conn:
        print("Step 1: creating event_targets...")
        if not await _table_exists(conn, "event_targets"):
            await conn.execute(text(
                """
                CREATE TABLE event_targets (
                    id                       SERIAL PRIMARY KEY,
                    event_id                 INTEGER NOT NULL REFERENCES event_definitions(id) ON DELETE CASCADE,
                    tenant_id                INTEGER NOT NULL REFERENCES tenants(id),
                    notification_channel_id  TEXT NOT NULL DEFAULT '',
                    notification_role_id     TEXT NOT NULL DEFAULT '',
                    CONSTRAINT uq_event_target UNIQUE (event_id, tenant_id)
                )
                """
            ))
            print("  done")
        else:
            print("  already present, skipping")

    await engine.dispose()
    print("\nMigration complete. Every existing event now has zero explicit")
    print("extra targets — unchanged behavior (owning tenant + automatic")
    print("kingdom-wide fan-out only). Deploy the new application code next.")


if __name__ == "__main__":
    asyncio.run(migrate())
