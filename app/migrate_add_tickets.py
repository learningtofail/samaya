"""One-time migration: creates the `tickets` and `ticket_votes` tables
(spec §40-43 — the public feedback/request/error-report board at
/feedback, with anonymous per-ticket upvoting).

Run this ONCE against production, after taking a backup, BEFORE deploying
the new application code (the new models/db.py expects these tables to
already exist). Not wired into Alembic — see migrate_to_multitenant.py's
own docstring for why this repo hand-writes migrations instead.

Usage:
    cd app && python migrate_add_tickets.py

Safe to re-run: checks whether each table already exists before creating
it, so an interrupted run can just be re-run.

What it does:
  Creates `tickets` (id, kind, error_type, title, description, tenant_id
  FK -> tenants (nullable), related_occurrence_id FK -> occurrences
  (nullable), related_announcement_id FK -> announcements (nullable),
  submitter_contact, status default 'open', upvote_count default 1,
  created_at/updated_at) and `ticket_votes` (id, ticket_id FK -> tickets
  ON DELETE CASCADE, voter_key, created_at, unique on (ticket_id,
  voter_key)). No existing data is touched — this is a brand new feature
  with no prior rows anywhere.
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
        print("Step 1: creating tickets...")
        if not await _table_exists(conn, "tickets"):
            await conn.execute(text(
                """
                CREATE TABLE tickets (
                    id                        SERIAL PRIMARY KEY,
                    kind                      TEXT NOT NULL,
                    error_type                TEXT,
                    title                     TEXT NOT NULL,
                    description               TEXT NOT NULL,
                    tenant_id                 INTEGER REFERENCES tenants(id),
                    related_occurrence_id     INTEGER REFERENCES occurrences(id),
                    related_announcement_id   INTEGER REFERENCES announcements(id),
                    submitter_contact         TEXT,
                    status                    TEXT NOT NULL DEFAULT 'open',
                    upvote_count              INTEGER NOT NULL DEFAULT 1,
                    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
                    CONSTRAINT ck_ticket_kind CHECK (kind IN ('feedback', 'event_request', 'announcement_request', 'error')),
                    CONSTRAINT ck_ticket_status CHECK (status IN ('open', 'planned', 'in_progress', 'done', 'declined')),
                    CONSTRAINT ck_ticket_related_mutually_exclusive CHECK (
                        related_occurrence_id IS NULL OR related_announcement_id IS NULL
                    )
                )
                """
            ))
            print("  done")
        else:
            print("  already present, skipping")

        print("Step 2: creating ticket_votes...")
        if not await _table_exists(conn, "ticket_votes"):
            await conn.execute(text(
                """
                CREATE TABLE ticket_votes (
                    id         SERIAL PRIMARY KEY,
                    ticket_id  INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
                    voter_key  TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    CONSTRAINT uq_ticket_vote UNIQUE (ticket_id, voter_key)
                )
                """
            ))
            print("  done")
        else:
            print("  already present, skipping")

    await engine.dispose()
    print("\nMigration complete. The public feedback board (/feedback) and its")
    print("admin Tickets tab are now backed by real tables. Deploy the new")
    print("application code next.")


if __name__ == "__main__":
    asyncio.run(migrate())
