"""
Tests for public-facing endpoints (/api/events, /ics/events.ics).
Verifies leadership_only events are excluded from both.
"""
from datetime import date, datetime, time, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import EventDefinition, Occurrence


async def _make_event_with_occurrence(
    db_session: AsyncSession,
    name: str,
    leadership_only: bool,
    active: bool = True,
) -> tuple[EventDefinition, Occurrence]:
    today = date.today()
    event = EventDefinition(
        name=name,
        interval_days=7,
        start_time_utc=time(19, 0),
        duration_hours=1.0,
        discord_channel="#test",
        description="Test event",
        category="Other",
        alliance="Server",
        leadership_only=leadership_only,
        active=active,
        anchor_date=today,
        notification_channel_id="",
        notification_role_id="",
    )
    db_session.add(event)
    await db_session.commit()
    await db_session.refresh(event)

    start_dt = datetime.combine(today, time(19, 0), tzinfo=timezone.utc)
    occ = Occurrence(
        event_id=event.id,
        occurrence_date=today,
        start_datetime_utc=start_dt,
        end_datetime_utc=start_dt + timedelta(hours=1),
        post_to_discord=True,
        post_status="pending",
    )
    db_session.add(occ)
    await db_session.commit()
    await db_session.refresh(occ)

    return event, occ


class TestPublicEventsExcludeLeadership:

    async def test_community_event_appears(self, client: AsyncClient, db_session: AsyncSession):
        await _make_event_with_occurrence(db_session, "Community Event", leadership_only=False)
        r = await client.get("/api/events")
        assert r.status_code == 200
        names = [e["event_name"] for e in r.json()]
        assert "Community Event" in names

    async def test_leadership_event_excluded(self, client: AsyncClient, db_session: AsyncSession):
        await _make_event_with_occurrence(db_session, "Leadership Event", leadership_only=True)
        r = await client.get("/api/events")
        assert r.status_code == 200
        names = [e["event_name"] for e in r.json()]
        assert "Leadership Event" not in names

    async def test_mixed_events_only_community_returned(self, client: AsyncClient, db_session: AsyncSession):
        await _make_event_with_occurrence(db_session, "Community Event", leadership_only=False)
        await _make_event_with_occurrence(db_session, "Leadership Event", leadership_only=True)
        r = await client.get("/api/events")
        names = [e["event_name"] for e in r.json()]
        assert "Community Event" in names
        assert "Leadership Event" not in names


class TestICSFeedExcludesLeadership:

    async def test_community_event_in_ics(self, client: AsyncClient, db_session: AsyncSession):
        await _make_event_with_occurrence(db_session, "Community Event", leadership_only=False)
        r = await client.get("/ics/events.ics")
        assert r.status_code == 200
        assert "Community Event" in r.text

    async def test_leadership_event_not_in_ics(self, client: AsyncClient, db_session: AsyncSession):
        await _make_event_with_occurrence(db_session, "Leadership Event", leadership_only=True)
        r = await client.get("/ics/events.ics")
        assert r.status_code == 200
        assert "Leadership Event" not in r.text

    async def test_mixed_events_only_community_in_ics(self, client: AsyncClient, db_session: AsyncSession):
        await _make_event_with_occurrence(db_session, "Community Event", leadership_only=False)
        await _make_event_with_occurrence(db_session, "Leadership Event", leadership_only=True)
        r = await client.get("/ics/events.ics")
        assert "Community Event" in r.text
        assert "Leadership Event" not in r.text
