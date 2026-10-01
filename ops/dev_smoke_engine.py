"""Smoke test of the unified delivery engine against REAL Discord.

Run it on the dev machine only, with the dev .env (dev bot token, dev
database, test server). It creates one throwaway event, lets the engine post
its Discord Scheduled Event and one reminder, shows the delivery rows, waits
for you to check Discord, then removes everything it made.

    cd app && python ../ops/dev_smoke_engine.py <tenant-slug>

Prerequisites: the tenant's Discord server row holds the test guild ID, and
the tenant has a notification channel set (Tenant.notification_channel_id).
"""
import asyncio
import os
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import selectinload  # noqa: E402

from models import AsyncSessionLocal  # noqa: E402
from models.db import Delivery, Event, EventAlliance, EventOccurrence, EventReminder, EventType, Tenant  # noqa: E402
from services import discord_api  # noqa: E402
from services.event_engine import (  # noqa: E402
    platform_bot_token, remove_event_from_discord, run_delivery_tick, sync_event_occurrences,
)

EVENT_NAME = "Samaya smoke test"


async def show(title: str) -> None:
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(Delivery).join(EventOccurrence, EventOccurrence.id == Delivery.occurrence_id)
            .join(Event, Event.id == EventOccurrence.event_id).where(Event.name == EVENT_NAME).order_by(Delivery.id)
        )).scalars().all()
    print(f"\n{title}")
    for d in rows:
        print(f"  #{d.id} {d.kind:13} status={d.status:9} discord_event_id={d.discord_event_id} detail={d.detail}")


async def main(slug: str) -> int:
    if not platform_bot_token():
        print("PLATFORM_BOT_TOKEN is not set. Load the dev .env first.")
        return 1
    async with AsyncSessionLocal() as s:
        tenant = (await s.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()
        if tenant is None:
            print(f"No tenant with slug {slug!r}")
            return 1
        if not tenant.notification_channel_id:
            print("Set the tenant's notification channel first.")
            return 1
        print(f"Tenant {tenant.name}, guild {tenant.server.guild_id}")
        if input("Is this the TEST guild, not a real alliance server? Type yes: ").strip().lower() != "yes":
            return 1
        etype = (await s.execute(select(EventType).where(EventType.kingdom_id == tenant.kingdom_id))).scalars().first()
        start = (datetime.now(timezone.utc) + timedelta(minutes=20)).replace(second=0, microsecond=0)
        event = Event(
            owning_tenant_id=tenant.id, type_id=etype.id, name=EVENT_NAME, scope="alliance",
            message="Smoke test for {alliance_name}, starts {event_time_relative}",
            start_time_utc=time(start.hour, start.minute), duration_hours=1, recurrence_kind="none",
            anchor_date=start.date(), mention_role=False,
        )
        event.reminders = [EventReminder(minutes_before=19)]  # due about a minute from now
        event.alliances = [EventAlliance(tenant_id=tenant.id)]
        s.add(event)
        await s.commit()
        event_id = event.id

    try:
        async with AsyncSessionLocal() as s:
            event = (await s.execute(select(Event).options(selectinload(Event.reminders))
                                     .where(Event.id == event_id))).scalar_one()
            await sync_event_occurrences(s, event, datetime.now(timezone.utc))
            await s.commit()
        await show("After generation")
        print("tick 1:", await run_delivery_tick(AsyncSessionLocal, discord_api))
        await show("After tick 1 (expect the discord_event posted)")
        print("\nWaiting 75 seconds for the reminder to come due...")
        await asyncio.sleep(75)
        print("tick 2:", await run_delivery_tick(AsyncSessionLocal, discord_api))
        await show("After tick 2 (expect the reminder posted)")
        input("\nCheck the test server: one Scheduled Event and one channel message. Press Enter to clean up.")
    finally:
        async with AsyncSessionLocal() as s:
            errors = await remove_event_from_discord(s, discord_api, event_id)
            event = await s.get(Event, event_id)
            if event is not None:
                await s.delete(event)
            await s.commit()
        print("Cleanup done." + (f" Discord errors: {errors}" if errors else ""))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    if "DATABASE_URL" not in os.environ:
        print("DATABASE_URL is not set; refusing to guess a database.")
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))
