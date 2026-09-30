"""Tests for spec §37: scheduled/posted Announcements surfaced on the
public events page (per-tenant /t/{slug}/api/events and combined
/api/events), tagged "kind": "announcement" and excluded from the ICS
feed, plus notify_minutes_before (events) / event_offset_minutes
(announcements) exposed for the "how far ahead does the notification go
out" display.
"""
from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import Announcement, AnnouncementTarget, EventDefinition


async def _make_announcement(
    db_session: AsyncSession,
    owning_tenant_id: int,
    target_tenant_id: int,
    title: str,
    status: str = "scheduled",
    leadership_only: bool = False,
    scheduled_for=None,
    event_offset_minutes: int = 0,
) -> Announcement:
    ann = Announcement(
        owning_tenant_id=owning_tenant_id,
        title=title,
        body_markdown="Body text for " + title,
        scheduled_for=scheduled_for or (datetime.now(timezone.utc) + timedelta(hours=2)),
        status=status,
        leadership_only=leadership_only,
        event_offset_minutes=event_offset_minutes,
    )
    db_session.add(ann)
    await db_session.commit()
    await db_session.refresh(ann)
    db_session.add(AnnouncementTarget(
        announcement_id=ann.id, tenant_id=target_tenant_id,
        discord_channel_id="123456", post_status="pending" if status == "scheduled" else "posted",
    ))
    await db_session.commit()
    return ann


class TestPublicEventsIncludeAnnouncements:

    async def test_scheduled_announcement_appears_for_its_target_tenant(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict
    ):
        await _make_announcement(db_session, tenant["id"], tenant["id"], "Daily Reset Warning")
        r = await client.get(f"/t/{tenant['slug']}/api/events")
        assert r.status_code == 200
        rows = [e for e in r.json() if e["event_name"] == "Daily Reset Warning"]
        assert len(rows) == 1
        assert rows[0]["kind"] == "announcement"
        assert rows[0]["post_status"] == "scheduled"

    async def test_posted_announcement_also_appears(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict
    ):
        await _make_announcement(db_session, tenant["id"], tenant["id"], "Already Sent", status="posted")
        r = await client.get(f"/t/{tenant['slug']}/api/events")
        names = [e["event_name"] for e in r.json()]
        assert "Already Sent" in names

    async def test_draft_announcement_excluded(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict
    ):
        await _make_announcement(db_session, tenant["id"], tenant["id"], "Draft Only", status="draft")
        r = await client.get(f"/t/{tenant['slug']}/api/events")
        names = [e["event_name"] for e in r.json()]
        assert "Draft Only" not in names

    async def test_cancelled_announcement_excluded(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict
    ):
        await _make_announcement(db_session, tenant["id"], tenant["id"], "Cancelled One", status="cancelled")
        r = await client.get(f"/t/{tenant['slug']}/api/events")
        names = [e["event_name"] for e in r.json()]
        assert "Cancelled One" not in names

    async def test_leadership_only_announcement_excluded(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict
    ):
        await _make_announcement(db_session, tenant["id"], tenant["id"], "Leaders Only", leadership_only=True)
        r = await client.get(f"/t/{tenant['slug']}/api/events")
        names = [e["event_name"] for e in r.json()]
        assert "Leaders Only" not in names

    async def test_announcement_outside_window_excluded(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict
    ):
        far_future = datetime.now(timezone.utc) + timedelta(days=60)
        await _make_announcement(db_session, tenant["id"], tenant["id"], "Way Later", scheduled_for=far_future)
        r = await client.get(f"/t/{tenant['slug']}/api/events")
        names = [e["event_name"] for e in r.json()]
        assert "Way Later" not in names

    async def test_announcement_not_visible_to_untargeted_tenant(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, second_tenant: dict
    ):
        await _make_announcement(db_session, tenant["id"], tenant["id"], "MOD Only Announcement")
        r = await client.get(f"/t/{second_tenant['slug']}/api/events")
        names = [e["event_name"] for e in r.json()]
        assert "MOD Only Announcement" not in names

    async def test_event_offset_minutes_included(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict
    ):
        await _make_announcement(db_session, tenant["id"], tenant["id"], "Warn Ahead", event_offset_minutes=15)
        r = await client.get(f"/t/{tenant['slug']}/api/events")
        row = next(e for e in r.json() if e["event_name"] == "Warn Ahead")
        assert row["event_offset_minutes"] == 15

    async def test_combined_view_includes_announcements_with_tenant_badge(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict
    ):
        await _make_announcement(db_session, tenant["id"], tenant["id"], "Combined View Ann")
        r = await client.get("/api/events")
        row = next(e for e in r.json() if e["event_name"] == "Combined View Ann")
        assert row["kind"] == "announcement"
        assert row["tenant_slug"] == tenant["slug"]

    async def test_announcement_fanned_out_to_two_tenants_shows_once_each(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, second_tenant: dict
    ):
        ann = Announcement(
            owning_tenant_id=tenant["id"], title="Two Server Notice", body_markdown="hi",
            scheduled_for=datetime.now(timezone.utc) + timedelta(hours=1), status="scheduled",
        )
        db_session.add(ann)
        await db_session.commit()
        await db_session.refresh(ann)
        db_session.add_all([
            AnnouncementTarget(announcement_id=ann.id, tenant_id=tenant["id"], discord_channel_id="c1", post_status="pending"),
            AnnouncementTarget(announcement_id=ann.id, tenant_id=second_tenant["id"], discord_channel_id="c2", post_status="pending"),
        ])
        await db_session.commit()

        r = await client.get("/api/events")
        rows = [e for e in r.json() if e["event_name"] == "Two Server Notice"]
        assert len(rows) == 2
        assert {rows[0]["tenant_slug"], rows[1]["tenant_slug"]} == {tenant["slug"], second_tenant["slug"]}


class TestAnnouncementsExcludedFromIcs:

    async def test_scheduled_announcement_not_in_per_tenant_ics(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict
    ):
        await _make_announcement(db_session, tenant["id"], tenant["id"], "Should Not Sync")
        r = await client.get(f"/t/{tenant['slug']}/ics/events.ics")
        assert r.status_code == 200
        assert "Should Not Sync" not in r.text

    async def test_scheduled_announcement_not_in_combined_ics(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict
    ):
        await _make_announcement(db_session, tenant["id"], tenant["id"], "Also Should Not Sync")
        r = await client.get("/ics/events.ics")
        assert "Also Should Not Sync" not in r.text


class TestNotifyMinutesBeforeOnPublicEvents:

    async def test_event_notify_minutes_before_exposed(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict
    ):
        from datetime import date, time
        event = EventDefinition(
            owning_tenant_id=tenant["id"], scope="alliance", name="Reminder Event",
            interval_days=7, start_time_utc=time(19, 0), duration_hours=1.0,
            discord_channel="#test", description="", anchor_date=date.today(),
            notify_minutes_before=15,
        )
        db_session.add(event)
        await db_session.commit()
        await db_session.refresh(event)
        from models.db import Occurrence
        start_dt = datetime.now(timezone.utc) + timedelta(hours=1)
        occ = Occurrence(
            event_id=event.id, tenant_id=tenant["id"], occurrence_date=start_dt.date(),
            start_datetime_utc=start_dt, end_datetime_utc=start_dt + timedelta(hours=1),
            post_status="pending",
        )
        db_session.add(occ)
        await db_session.commit()

        r = await client.get(f"/t/{tenant['slug']}/api/events")
        row = next(e for e in r.json() if e["event_name"] == "Reminder Event")
        assert row["notify_minutes_before"] == 15
        assert row["kind"] == "event"
