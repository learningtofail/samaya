"""One-time migration: widens the ck_user_tenant_role and ck_invite_role
CHECK constraints to allow 'viewer' as a role value (spec §31 — read-only
Viewer tenant role).

Run this ONCE against production, after taking a backup, BEFORE deploying
the new application code (the new models/db.py declares both constraints
with 'viewer' already included, so a fresh CREATE TABLE would get the new
definition automatically — this script is only needed to bring an existing
database's constraints in line with it). Not wired into Alembic — see
migrate_to_multitenant.py's own docstring for why this repo hand-writes
migrations instead.

Usage:
    cd app && python migrate_add_viewer_role.py

Safe to re-run: reads each constraint's current definition via
pg_get_constraintdef and only drops/recreates it if 'viewer' isn't already
present, so an interrupted run (or running this after it's already been
applied) is a no-op.

What it does:
  1. ALTER TABLE user_tenants DROP/ADD CONSTRAINT ck_user_tenant_role to
     CHECK (role IN ('owner', 'coordinator', 'viewer')) — was ('owner',
     'coordinator').
  2. ALTER TABLE invites DROP/ADD CONSTRAINT ck_invite_role to CHECK (role
     IN ('owner', 'coordinator', 'viewer', 'kingdom_coordinator')) — was
     ('owner', 'coordinator', 'kingdom_coordinator').

No existing rows are affected — no UserTenant or Invite row has ever had
role='viewer' before the application code that can create one is deployed,
so widening the constraint is purely additive.
"""
import asyncio
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://taraka:password@db:5432/kingshot_scheduler"
)


async def _constraint_def(conn, constraint_name: str) -> str | None:
    result = await conn.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :name"
        ),
        {"name": constraint_name},
    )
    row = result.scalar()
    return row


async def migrate():
    engine = create_async_engine(DATABASE_URL, echo=False)

    async with engine.begin() as conn:
        print("Step 1: widening ck_user_tenant_role to include 'viewer'...")
        current = await _constraint_def(conn, "ck_user_tenant_role")
        if current is None:
            print("  constraint not found — nothing to widen (unexpected; check table name)")
        elif "viewer" in current:
            print("  already includes 'viewer', skipping")
        else:
            await conn.execute(text("ALTER TABLE user_tenants DROP CONSTRAINT ck_user_tenant_role"))
            await conn.execute(text(
                "ALTER TABLE user_tenants ADD CONSTRAINT ck_user_tenant_role "
                "CHECK (role IN ('owner', 'coordinator', 'viewer'))"
            ))
            print("  done")

        print("Step 2: widening ck_invite_role to include 'viewer'...")
        current = await _constraint_def(conn, "ck_invite_role")
        if current is None:
            print("  constraint not found — nothing to widen (unexpected; check table name)")
        elif "viewer" in current:
            print("  already includes 'viewer', skipping")
        else:
            await conn.execute(text("ALTER TABLE invites DROP CONSTRAINT ck_invite_role"))
            await conn.execute(text(
                "ALTER TABLE invites ADD CONSTRAINT ck_invite_role "
                "CHECK (role IN ('owner', 'coordinator', 'viewer', 'kingdom_coordinator'))"
            ))
            print("  done")

    await engine.dispose()
    print("\nMigration complete. 'viewer' is now a valid role value for both")
    print("user_tenant and invites. Deploy the new application code next.")


if __name__ == "__main__":
    asyncio.run(migrate())
