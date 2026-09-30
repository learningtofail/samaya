"""Spec §44 — a PostLog row that's still `posted` but has fallen out of
Discord's own scheduled-events listing should read as "this simply
finished" (a `naturally_completed` sync row) rather than "may have been
deleted directly in Discord" (`postlog_only`), whenever Samaya's own
schedule data shows the occurrence was already due to have ended. Discord
stops surfacing a COMPLETED scheduled event from that listing fairly
quickly, and routers/webhooks.py's GUILD_SCHEDULED_EVENT_UPDATE/_DELETE
handlers can never actually fire to tell this app that happened in real
time — those are Gateway dispatch payloads, only ever delivered over a
bot's persistent Gateway connection, never to an HTTP "Interactions
Endpoint URL" the way that file assumed. `get_guild_events` is
monkeypatched here (imported inside the function body in
routers/admin/discord_sync.py, so the patch target is the defining module,
not the importer) to simulate "Discord no longer lists this event" without
any real network call.
"""
from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import EventDefinition, PostLog


async def _fake_get_guild_events(*a, **k):
    return []


async def _seed_event_and_log(db_session: AsyncSession, tenant: dict, *, hours_ago_ended: float):
    """An EventDefinition/PostLog pair whose occurrence ended `hours_ago_ended`
    hours before "now" (negative means it hasn't ended yet)."""
    now = datetime.now(timezone.utc)
    occurrence_end = now - timedelta(hours=hours_ago_ended)
    duration_hours = 1.0
    occurrence_start = occurrence_end - timedelta(hours=duration_hours)

    event = EventDefinition(
        owning_tenant_id=tenant["id"], scope="alliance",
        name="Bear Hunt @ Trap 1", interval_days=1,
        start_time_utc=occurrence_start.time().replace(microsecond=0),
        duration_hours=duration_hours, anchor_date=occurrence_start.date(),
        discord_channel="events",
    )
    db_session.add(event)
    await db_session.commit()
    await db_session.refresh(event)

    log = PostLog(
        tenant_id=tenant["id"], event_id=event.id, event_name=event.name,
        occurrence_date=occurrence_start.date(), discord_event_id="discord-evt-1",
        discord_guild_id="test-guild-mod", posted_at_utc=occurrence_start,
        posted_by="system", status="posted",
    )
    db_session.add(log)
    await db_session.commit()
    await db_session.refresh(log)
    return event, log


class TestNaturallyCompletedClassification:

    async def test_ended_occurrence_missing_from_discord_is_naturally_completed(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, monkeypatch
    ):
        monkeypatch.setattr("services.discord_api.get_guild_events", _fake_get_guild_events)
        event, log = await _seed_event_and_log(db_session, tenant, hours_ago_ended=3)

        r = await client.get("/admin/api/sync/discord", headers={"X-Tenant-Slug": tenant["slug"]})
        assert r.status_code == 200
        body = r.json()["alliances"][0]

        assert body["summary"]["naturally_completed"] == 1
        assert body["summary"]["postlog_only"] == 0
        # The whole point: this must not count toward the red "issues"
        # badge the Sync tab surfaces as an alert.
        assert body["summary"]["issues"] == 0
        assert body["naturally_completed"][0]["post_log_id"] == log.id

    async def test_not_yet_ended_occurrence_missing_from_discord_is_still_postlog_only(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, monkeypatch
    ):
        monkeypatch.setattr("services.discord_api.get_guild_events", _fake_get_guild_events)
        event, log = await _seed_event_and_log(db_session, tenant, hours_ago_ended=-3)

        r = await client.get("/admin/api/sync/discord", headers={"X-Tenant-Slug": tenant["slug"]})
        body = r.json()["alliances"][0]

        assert body["summary"]["postlog_only"] == 1
        assert body["summary"]["naturally_completed"] == 0
        assert body["summary"]["issues"] == 1

    async def test_mark_completed_retires_the_row_from_future_syncs(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, monkeypatch
    ):
        monkeypatch.setattr("services.discord_api.get_guild_events", _fake_get_guild_events)
        event, log = await _seed_event_and_log(db_session, tenant, hours_ago_ended=3)

        r = await client.post(f"/admin/api/sync/mark-completed/{log.id}", headers={"X-Tenant-Slug": tenant["slug"]})
        assert r.status_code == 200

        await db_session.refresh(log)
        assert log.status == "completed"

        r2 = await client.get("/admin/api/sync/discord", headers={"X-Tenant-Slug": tenant["slug"]})
        body = r2.json()["alliances"][0]
        assert body["summary"]["naturally_completed"] == 0
        assert body["summary"]["postlog_only"] == 0

    async def test_mark_completed_unknown_id_404s(
        self, client: AsyncClient, tenant: dict
    ):
        r = await client.post("/admin/api/sync/mark-completed/999999", headers={"X-Tenant-Slug": tenant["slug"]})
        assert r.status_code == 404


class TestLastActivityIncludesCompleted:

    async def test_completed_postlog_row_still_counts_as_last_activity(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict
    ):
        now = datetime.now(timezone.utc)
        log = PostLog(
            tenant_id=tenant["id"], event_name="Bear Hunt @ Trap 1",
            occurrence_date=now.date(), discord_event_id="discord-evt-2",
            discord_guild_id="test-guild-mod", posted_at_utc=now,
            posted_by="system", status="completed",
        )
        db_session.add(log)
        await db_session.commit()

        r = await client.get(f"/t/{tenant['slug']}/api/last-activity")
        assert r.status_code == 200
        body = r.json()
        assert body is not None
        assert body["name"] == "Bear Hunt @ Trap 1"
        assert body["kind"] == "event"
