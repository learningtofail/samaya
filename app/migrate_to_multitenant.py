"""One-time migration: singleton DiscordConfig -> multi-tenant schema,
plus the Phase 4 (real auth) and Phase 5 (scheduled announcements) tables
— all three phases were built before this was ever run against
production, so this is the single combined migration for all of them,
not three separate ones.

Run this ONCE against production, after taking a backup, BEFORE deploying
the new application code (the new models/db.py expects the columns this
script adds to already exist). Not wired into Alembic — despite being a
listed dependency, this repo has never actually used Alembic; the app has
only ever relied on Base.metadata.create_all() at startup, which creates
missing tables but never alters existing ones. That gap isn't fixed here;
this script is a deliberately narrow, one-time bridge for this specific
change; consider setting up real Alembic migrations before the *next*
schema change, so this isn't repeated.

Usage:
    cd app && python migrate_to_multitenant.py

Safe to re-run: every step checks whether it already happened before
doing anything (see _column_exists / _table_exists below), so an
interrupted run can just be re-run rather than requiring a restore.

What it does, in order:
  1. Creates kingdoms, tenants, event_tenant_notifications (the tables
     Base.metadata.create_all() would create anyway on next app startup —
     done explicitly here first so the backfill steps below have
     something to backfill into).
  2. Seeds one Kingdom ("Kingdom 138") and two Tenant rows (MOD, NSR),
     both copying guild_id/bot_token/public_key from the single existing
     discord_config row — they share one Discord server today, so they
     share one set of credentials, same as before this migration.
  3. Adds owning_tenant_id + scope to event_definitions, backfills from
     the old alliance column (M0D->MOD tenant, NSR->NSR tenant, Server->
     scope='kingdom-wide' with MOD as the owning tenant — arbitrary but
     necessary, since scope didn't distinguish an "owner" from the "every
     alliance" case before; nothing observable changes for members, since
     posting for an already-kingdom-wide event fans out the same either
     way), then drops the old category/alliance columns.
  4. Adds tenant_id to occurrences, backfilling from each row's own
     event's (now-set) owning_tenant_id.
  5. Adds tenant_id to post_log, backfilling the same way where event_id
     is still set; for rows where event_id is already null (a
     permanently-deleted event — see routers/admin/events.py's
     permanent_delete_event, which nulls event_id but keeps event_name),
     there is no reliable way to recover which alliance a historical row
     belonged to, since the old schema never tracked that on PostLog
     itself — those default to the MOD tenant, same arbitrary-but-
     necessary call as step 3. Replaces the old uq_post_log constraint
     (event_name, occurrence_date) with (tenant_id, event_name,
     occurrence_date).
  6. Drops scheduler_state and lets the app recreate it fresh, per-tenant,
     on first run — it's operational status (last run time/result), not
     business data, and the old single global row can't be attributed to
     one specific tenant retroactively.
  7. Drops discord_config — fully superseded by tenants.bot_token/guild_id.
  8. Creates users, user_tenants, user_kingdoms, invites, audit_log
     (Phase 4 — real auth). Pure CREATE TABLE, no backfill: these are
     brand new, so there's no existing data to migrate into them. No
     superadmin is seeded here — the first one is granted by
     SUPERADMIN_DISCORD_IDS in .env on their first login, same as
     ADMIN_API_KEY used to work before Phase 4 replaced it.
  9. Creates announcements, announcement_targets (Phase 5 — scheduled
     announcements). Also pure CREATE TABLE, same reasoning as step 8.
"""
import asyncio
import os
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://taraka:password@db:5432/kingshot_scheduler"
)


async def _table_exists(conn, table_name: str) -> bool:
    result = await conn.execute(
        text("SELECT to_regclass(:name)"), {"name": table_name}
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
        if not await _table_exists(conn, "discord_config"):
            print("discord_config not found — this looks like a fresh database "
                  "with no legacy data to migrate. Nothing to do; the app's normal "
                  "startup (Base.metadata.create_all) will create the new schema "
                  "directly. Exiting.")
            return

        # ---- Step 1: new tables ----
        print("Step 1: creating kingdoms, tenants, event_tenant_notifications...")
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS kingdoms (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                slug TEXT NOT NULL UNIQUE
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS tenants (
                id SERIAL PRIMARY KEY,
                kingdom_id INTEGER NOT NULL REFERENCES kingdoms(id),
                name TEXT NOT NULL,
                slug TEXT NOT NULL UNIQUE,
                guild_id TEXT NOT NULL,
                bot_token TEXT,
                public_key TEXT,
                color TEXT NOT NULL DEFAULT '#475569',
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS event_tenant_notifications (
                id SERIAL PRIMARY KEY,
                event_id INTEGER NOT NULL REFERENCES event_definitions(id) ON DELETE CASCADE,
                tenant_id INTEGER NOT NULL REFERENCES tenants(id),
                notification_channel_id TEXT NOT NULL DEFAULT '',
                notification_role_id TEXT NOT NULL DEFAULT '',
                UNIQUE (event_id, tenant_id)
            )
        """))

        # ---- Step 2: seed Kingdom + two Tenants from the old singleton config ----
        print("Step 2: seeding Kingdom 138 and MOD/NSR tenants from discord_config...")
        cfg_row = await conn.execute(text(
            "SELECT bot_token, guild_id, public_key FROM discord_config LIMIT 1"
        ))
        cfg = cfg_row.mappings().first()
        if cfg is None:
            sys.exit("discord_config table exists but has no row — cannot seed "
                     "tenants without a guild_id. Configure Discord via the old "
                     "admin UI first, or edit this script to hardcode one.")

        existing_kingdom = await conn.execute(
            text("SELECT id FROM kingdoms WHERE slug = 'k138'")
        )
        kingdom_id = existing_kingdom.scalar()
        if kingdom_id is None:
            result = await conn.execute(
                text("INSERT INTO kingdoms (name, slug) VALUES ('Kingdom 138', 'k138') RETURNING id")
            )
            kingdom_id = result.scalar()
            print(f"  created Kingdom 138 (id={kingdom_id})")
        else:
            print(f"  Kingdom 138 already exists (id={kingdom_id})")

        tenant_ids = {}
        for name, slug in (("MOD", "mod"), ("NSR", "nsr")):
            existing = await conn.execute(text("SELECT id FROM tenants WHERE slug = :slug"), {"slug": slug})
            tid = existing.scalar()
            if tid is None:
                result = await conn.execute(
                    text(
                        "INSERT INTO tenants (kingdom_id, name, slug, guild_id, bot_token, public_key) "
                        "VALUES (:kid, :name, :slug, :guild_id, :bot_token, :public_key) RETURNING id"
                    ),
                    {
                        "kid": kingdom_id, "name": name, "slug": slug,
                        "guild_id": cfg["guild_id"], "bot_token": cfg["bot_token"],
                        "public_key": cfg["public_key"],
                    },
                )
                tid = result.scalar()
                print(f"  created tenant {name} (id={tid})")
            else:
                print(f"  tenant {name} already exists (id={tid})")
            tenant_ids[name] = tid

        mod_id, nsr_id = tenant_ids["MOD"], tenant_ids["NSR"]

        # ---- Step 3: event_definitions ----
        print("Step 3: migrating event_definitions...")
        if not await _column_exists(conn, "event_definitions", "owning_tenant_id"):
            await conn.execute(text("ALTER TABLE event_definitions ADD COLUMN owning_tenant_id INTEGER"))
            await conn.execute(text("ALTER TABLE event_definitions ADD COLUMN scope TEXT NOT NULL DEFAULT 'alliance'"))

            await conn.execute(text(
                "UPDATE event_definitions SET owning_tenant_id = :mod, scope = 'alliance' "
                "WHERE alliance = 'M0D'"
            ), {"mod": mod_id})
            await conn.execute(text(
                "UPDATE event_definitions SET owning_tenant_id = :nsr, scope = 'alliance' "
                "WHERE alliance = 'NSR'"
            ), {"nsr": nsr_id})
            # 'Server' events become kingdom-wide, owned by MOD (arbitrary —
            # see module docstring for why this doesn't change member-visible
            # behavior).
            await conn.execute(text(
                "UPDATE event_definitions SET owning_tenant_id = :mod, scope = 'kingdom-wide' "
                "WHERE alliance = 'Server' OR alliance IS NULL"
            ), {"mod": mod_id})

            await conn.execute(text("ALTER TABLE event_definitions ALTER COLUMN owning_tenant_id SET NOT NULL"))
            await conn.execute(text(
                "ALTER TABLE event_definitions ADD CONSTRAINT fk_event_owning_tenant "
                "FOREIGN KEY (owning_tenant_id) REFERENCES tenants(id)"
            ))
            await conn.execute(text(
                "ALTER TABLE event_definitions ADD CONSTRAINT ck_scope_valid "
                "CHECK (scope IN ('alliance', 'kingdom-wide'))"
            ))
            await conn.execute(text("ALTER TABLE event_definitions DROP COLUMN IF EXISTS category"))
            await conn.execute(text("ALTER TABLE event_definitions DROP COLUMN IF EXISTS alliance"))
            print("  done")
        else:
            print("  already migrated, skipping")

        # ---- Step 4: occurrences ----
        print("Step 4: migrating occurrences...")
        if not await _column_exists(conn, "occurrences", "tenant_id"):
            await conn.execute(text("ALTER TABLE occurrences ADD COLUMN tenant_id INTEGER"))
            await conn.execute(text("""
                UPDATE occurrences o
                SET tenant_id = e.owning_tenant_id
                FROM event_definitions e
                WHERE o.event_id = e.id
            """))
            await conn.execute(text("ALTER TABLE occurrences ALTER COLUMN tenant_id SET NOT NULL"))
            await conn.execute(text(
                "ALTER TABLE occurrences ADD CONSTRAINT fk_occurrence_tenant "
                "FOREIGN KEY (tenant_id) REFERENCES tenants(id)"
            ))
            print("  done")
        else:
            print("  already migrated, skipping")

        # ---- Step 5: post_log ----
        print("Step 5: migrating post_log...")
        if not await _column_exists(conn, "post_log", "tenant_id"):
            await conn.execute(text("ALTER TABLE post_log ADD COLUMN tenant_id INTEGER"))
            await conn.execute(text("""
                UPDATE post_log p
                SET tenant_id = e.owning_tenant_id
                FROM event_definitions e
                WHERE p.event_id = e.id
            """))
            # Rows whose event was permanently deleted (event_id already
            # null) have no event to backfill from — no reliable way to
            # recover the original alliance, see module docstring.
            await conn.execute(text(
                "UPDATE post_log SET tenant_id = :mod WHERE tenant_id IS NULL"
            ), {"mod": mod_id})
            await conn.execute(text("ALTER TABLE post_log ALTER COLUMN tenant_id SET NOT NULL"))
            await conn.execute(text(
                "ALTER TABLE post_log ADD CONSTRAINT fk_post_log_tenant "
                "FOREIGN KEY (tenant_id) REFERENCES tenants(id)"
            ))
            await conn.execute(text("ALTER TABLE post_log DROP CONSTRAINT IF EXISTS uq_post_log"))
            await conn.execute(text(
                "ALTER TABLE post_log ADD CONSTRAINT uq_post_log "
                "UNIQUE (tenant_id, event_name, occurrence_date)"
            ))
            print("  done")
        else:
            print("  already migrated, skipping")

        # ---- Step 6: scheduler_state ----
        print("Step 6: resetting scheduler_state (per-tenant rows recreated on next run)...")
        if await _table_exists(conn, "scheduler_state") and not await _column_exists(conn, "scheduler_state", "tenant_id"):
            await conn.execute(text("DROP TABLE scheduler_state"))
            await conn.execute(text("""
                CREATE TABLE scheduler_state (
                    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
                    job_name TEXT NOT NULL,
                    last_run_utc TIMESTAMPTZ,
                    last_result TEXT,
                    last_detail TEXT,
                    next_run_utc TIMESTAMPTZ,
                    PRIMARY KEY (tenant_id, job_name)
                )
            """))
            print("  done — the app's startup overdue-check will run regeneration "
                  "for both tenants on next boot, since there's no prior state")
        else:
            print("  already migrated or doesn't exist, skipping")

        # ---- Step 7: drop discord_config ----
        print("Step 7: dropping discord_config...")
        await conn.execute(text("DROP TABLE IF EXISTS discord_config"))
        print("  done")

        # ---- Step 8: Phase 4 auth tables ----
        print("Step 8: creating users, user_tenants, user_kingdoms, invites, audit_log...")
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                discord_id TEXT NOT NULL UNIQUE,
                discord_username TEXT NOT NULL,
                is_superadmin BOOLEAN NOT NULL DEFAULT false,
                created_at TIMESTAMPTZ DEFAULT now(),
                last_login_at TIMESTAMPTZ
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS user_tenants (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                tenant_id INTEGER NOT NULL REFERENCES tenants(id),
                role TEXT NOT NULL CHECK (role IN ('owner', 'coordinator')),
                created_at TIMESTAMPTZ DEFAULT now(),
                UNIQUE (user_id, tenant_id)
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS user_kingdoms (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                kingdom_id INTEGER NOT NULL REFERENCES kingdoms(id),
                created_at TIMESTAMPTZ DEFAULT now(),
                UNIQUE (user_id, kingdom_id)
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS invites (
                id SERIAL PRIMARY KEY,
                token TEXT NOT NULL UNIQUE,
                tenant_id INTEGER REFERENCES tenants(id),
                kingdom_id INTEGER REFERENCES kingdoms(id),
                role TEXT NOT NULL CHECK (role IN ('owner', 'coordinator', 'kingdom_coordinator')),
                created_by INTEGER REFERENCES users(id),
                expires_at TIMESTAMPTZ NOT NULL,
                used_at TIMESTAMPTZ,
                used_by INTEGER REFERENCES users(id),
                revoked_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT now(),
                CHECK (
                    (tenant_id IS NOT NULL AND kingdom_id IS NULL) OR
                    (tenant_id IS NULL AND kingdom_id IS NOT NULL)
                )
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER REFERENCES tenants(id),
                user_id INTEGER REFERENCES users(id),
                table_name TEXT NOT NULL,
                row_id INTEGER,
                action TEXT NOT NULL CHECK (action IN ('create', 'update', 'delete')),
                before TEXT,
                after TEXT,
                timestamp TIMESTAMPTZ DEFAULT now()
            )
        """))
        print("  done — no superadmin seeded here; the first login from a "
              "Discord ID listed in SUPERADMIN_DISCORD_IDS (.env) grants it")

        # ---- Step 9: Phase 5 announcement tables ----
        print("Step 9: creating announcements, announcement_targets...")
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS announcements (
                id SERIAL PRIMARY KEY,
                owning_tenant_id INTEGER NOT NULL REFERENCES tenants(id),
                title TEXT NOT NULL,
                body_markdown TEXT NOT NULL,
                scheduled_for TIMESTAMPTZ NOT NULL,
                status TEXT NOT NULL DEFAULT 'scheduled'
                    CHECK (status IN ('draft', 'scheduled', 'posted', 'failed', 'cancelled')),
                created_by INTEGER REFERENCES users(id),
                created_at TIMESTAMPTZ DEFAULT now(),
                posted_at TIMESTAMPTZ
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS announcement_targets (
                id SERIAL PRIMARY KEY,
                announcement_id INTEGER NOT NULL REFERENCES announcements(id) ON DELETE CASCADE,
                tenant_id INTEGER NOT NULL REFERENCES tenants(id),
                discord_channel_id TEXT NOT NULL,
                discord_message_id TEXT,
                post_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (post_status IN ('pending', 'posted', 'error')),
                status_detail TEXT,
                UNIQUE (announcement_id, tenant_id)
            )
        """))
        print("  done")

    await engine.dispose()
    print("\nMigration complete.")
    print(f"MOD tenant id={mod_id}, slug=mod")
    print(f"NSR tenant id={nsr_id}, slug=nsr")
    print("\nNext, in order:")
    print("  1. Set SUPERADMIN_DISCORD_IDS, DISCORD_OAUTH_CLIENT_ID/_SECRET/_REDIRECT_URI,")
    print("     and SECRET_KEY in .env if not already set — the app refuses to boot")
    print("     without them now (see main.py's startup check).")
    print("  2. Deploy the new application code.")
    print("  3. Log in via Discord as one of the SUPERADMIN_DISCORD_IDS to confirm")
    print("     superadmin access, then verify both tenants load correctly in the")
    print("     admin UI before considering this done.")


if __name__ == "__main__":
    asyncio.run(migrate())
