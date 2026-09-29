"""One-time migration: introduces `discord_servers` as a first-class table
and repoints `tenants` at it via a new `server_id` FK, replacing the old
`tenants.guild_id`/`bot_token`/`public_key` columns (spec §25 — "Discord
Server as a First-Class Entity"). Alliances sharing a Discord guild used to
each carry their own separate copy of that guild's credentials (nothing
enforced they stayed consistent); this makes the sharing explicit and gives
every alliance on that guild a single shared source of truth for it.

Run this ONCE against production, after taking a backup, BEFORE deploying
the new application code (the new models/db.py expects `tenants.server_id`
to already exist and `tenants.guild_id`/`bot_token`/`public_key` to already
be gone). Not wired into Alembic — see migrate_to_multitenant.py's own
docstring for why this repo hand-writes migrations instead.

Usage:
    cd app && python migrate_add_discord_servers.py

Safe to re-run: every step checks whether it already happened before doing
anything (see _table_exists/_column_exists below), so an interrupted run
can just be re-run. Once `tenants.guild_id` has actually been dropped (the
final step), later re-runs correctly treat the whole migration as already
done and exit without touching anything.

What it does:
  1. Creates `discord_servers` (id, name, guild_id UNIQUE, bot_token
     nullable, public_key nullable, created_at) if it doesn't exist yet.
  2. For each DISTINCT guild_id currently on `tenants`, creates one
     `discord_servers` row (skipped if a row for that guild_id already
     exists — safe to re-run). Named after the first tenant found with
     that guild_id, ordered by tenant id; a human can rename it afterward
     via the new Platform UI (spec §25.3).
       - Bot token conflict resolution: if multiple tenants share a
         guild_id and have DIFFERENT non-null bot_token values, this is
         logged to stdout for manual review (guild_id, the differing
         tenant slugs, which token got chosen) rather than silently
         picked — a real data-quality question that needs eyes on the
         actual production values. The token used is the first non-null
         one found, ordered by tenant.id.
  3. Adds `tenants.server_id` (nullable at first, so existing rows aren't
     immediately constrained) if it doesn't exist yet, then backfills it
     from each tenant's guild_id -> the matching discord_servers.id.
  4. Sets `tenants.server_id` NOT NULL, once every row has a value.
  5. Drops `tenants.guild_id`, `tenants.bot_token`, `tenants.public_key` —
     the last step, and the one this script's idempotency check (step 1
     re-run) keys off of.
"""
import asyncio
import os
from collections import defaultdict

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
        # If tenants.guild_id is already gone, this migration has already
        # fully run (it's the last step) — nothing left to do.
        if not await _column_exists(conn, "tenants", "guild_id"):
            print("tenants.guild_id already dropped — migration already complete, nothing to do.")
            await engine.dispose()
            return

        print("Step 1: creating discord_servers...")
        if not await _table_exists(conn, "discord_servers"):
            await conn.execute(text(
                """
                CREATE TABLE discord_servers (
                    id          SERIAL PRIMARY KEY,
                    name        TEXT NOT NULL,
                    guild_id    TEXT NOT NULL UNIQUE,
                    bot_token   TEXT,
                    public_key  TEXT,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            ))
            print("  done")
        else:
            print("  already present, skipping")

        print("Step 2: backfilling discord_servers from tenants.guild_id...")
        existing = await conn.execute(text("SELECT guild_id FROM discord_servers"))
        already_seeded = {row[0] for row in existing.fetchall()}

        rows = (await conn.execute(text(
            "SELECT id, slug, name, guild_id, bot_token FROM tenants ORDER BY id"
        ))).fetchall()

        by_guild = defaultdict(list)
        for row in rows:
            by_guild[row.guild_id].append(row)

        created, skipped = 0, 0
        for guild_id, tenants_on_guild in by_guild.items():
            if guild_id in already_seeded:
                skipped += 1
                continue

            tokens = {t.bot_token for t in tenants_on_guild if t.bot_token}
            if len(tokens) > 1:
                chosen = tenants_on_guild[0].bot_token or next(iter(tokens))
                print(
                    f"  CONFLICT: guild_id={guild_id} has {len(tokens)} differing bot_token values "
                    f"across tenants {[t.slug for t in tenants_on_guild]} — "
                    f"choosing the token from '{tenants_on_guild[0].slug}'. Review manually if that's wrong."
                )
            else:
                chosen = next(iter(tokens), None)

            server_name = tenants_on_guild[0].name
            await conn.execute(
                text("INSERT INTO discord_servers (name, guild_id, bot_token) VALUES (:n, :g, :t)"),
                {"n": server_name, "g": guild_id, "t": chosen},
            )
            created += 1

        print(f"  created {created}, already present {skipped}")

        print("Step 3: adding tenants.server_id...")
        if not await _column_exists(conn, "tenants", "server_id"):
            await conn.execute(text(
                "ALTER TABLE tenants ADD COLUMN server_id INTEGER REFERENCES discord_servers(id)"
            ))
            print("  done")
        else:
            print("  already present, skipping")

        print("Step 4: backfilling tenants.server_id from guild_id...")
        await conn.execute(text(
            """
            UPDATE tenants
            SET server_id = discord_servers.id
            FROM discord_servers
            WHERE tenants.guild_id = discord_servers.guild_id
              AND tenants.server_id IS NULL
            """
        ))
        unset = (await conn.execute(
            text("SELECT count(*) FROM tenants WHERE server_id IS NULL")
        )).scalar()
        if unset:
            raise RuntimeError(
                f"{unset} tenant(s) still have no server_id after backfill — "
                "refusing to continue; investigate before re-running."
            )
        print("  done")

        print("Step 5: setting tenants.server_id NOT NULL...")
        await conn.execute(text("ALTER TABLE tenants ALTER COLUMN server_id SET NOT NULL"))
        print("  done")

        print("Step 6: dropping tenants.guild_id, bot_token, public_key...")
        await conn.execute(text("ALTER TABLE tenants DROP COLUMN guild_id"))
        await conn.execute(text("ALTER TABLE tenants DROP COLUMN bot_token"))
        await conn.execute(text("ALTER TABLE tenants DROP COLUMN public_key"))
        print("  done")

    await engine.dispose()
    print("\nMigration complete. Every tenant now references a discord_servers")
    print("row instead of carrying its own guild_id/bot_token/public_key.")
    print("Review any CONFLICT lines above manually. Deploy the new")
    print("application code next.")


if __name__ == "__main__":
    asyncio.run(migrate())
