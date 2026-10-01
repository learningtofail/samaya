"""Cutover step (spec §66.8): delete the Discord Scheduled Events the OLD
system created, so the new engine can create fresh ones.

Run once, after taking an ops/backup.sh backup and BEFORE `alembic upgrade
head` applies a1f0c0de0003 (which drops post_log, the only record of the
IDs). Inside the app container:

    docker compose exec app python cutover_delete_old_discord_events.py            # dry run
    docker compose exec app python cutover_delete_old_discord_events.py --apply

Only IDs recorded in post_log are touched; events created by hand in Discord
are never deleted. A 404 means the event is already gone and counts as
success. After each successful delete the ID is cleared from post_log, which
is what lets the migration's guard pass, so a partial run is safe to repeat.
"""
import asyncio
import os
import sys
from collections import defaultdict

from sqlalchemy import text

from models import AsyncSessionLocal
from services.discord_api import cancel_discord_event

QUERY = text("""
    SELECT p.id AS log_id, p.discord_event_id, p.event_name, p.occurrence_date,
           t.slug AS tenant_slug, s.guild_id, s.bot_token
    FROM post_log p
    JOIN tenants t ON t.id = p.tenant_id
    JOIN discord_servers s ON s.id = t.server_id
    WHERE p.discord_event_id IS NOT NULL
    ORDER BY s.guild_id, p.discord_event_id, p.id
""")


async def main(apply: bool) -> int:
    fallback = os.environ.get("PLATFORM_BOT_TOKEN", "")
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(QUERY)).mappings().all()
        # Two alliances on one Discord server share one real event (spec §52):
        # delete it once, then clear every post_log row that pointed at it.
        by_event: dict[tuple[str, str], list] = defaultdict(list)
        for row in rows:
            by_event[(row["guild_id"], row["discord_event_id"])].append(row)

        print(f"{len(rows)} post_log rows reference {len(by_event)} distinct Discord events.")
        if not apply:
            for (guild, event_id), group in by_event.items():
                first = group[0]
                print(f"  guild {guild} event {event_id}: {first['event_name']} {first['occurrence_date']}")
            print("Dry run. Re-run with --apply to delete them.")
            return 0

        failures = 0
        for (guild, event_id), group in by_event.items():
            token = group[0]["bot_token"] or fallback
            if not token:
                print(f"  SKIP guild {guild} event {event_id}: no bot token")
                failures += 1
                continue
            ok, error = await cancel_discord_event(token, guild, event_id)
            if not ok and not error.startswith("404"):
                print(f"  FAILED guild {guild} event {event_id}: {error}")
                failures += 1
                continue
            for row in group:
                await session.execute(text("UPDATE post_log SET discord_event_id = NULL WHERE id = :id"), {"id": row["log_id"]})
            await session.commit()
            print(f"  deleted guild {guild} event {event_id} ({'already gone' if not ok else 'ok'})")
        print(f"Done. {failures} failure(s).")
        return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--apply" in sys.argv)))
