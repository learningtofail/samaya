"""
Tests for public-facing endpoints (/t/{slug}/api/events, /t/{slug}/ics/events.ics).
Verifies leadership_only events are excluded from both, and that a
tenant's public feed shows its own events plus kingdom-wide ones but not
another tenant's alliance-only events.
"""
from datetime import date, datetime, time, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import EventDefinition, Occurrence


async def _make_event_with_occurrence(
    db_session: AsyncSession,
    tenant_id: int,
    name: str,
    leadership_only: bool,
    active: bool = True,
    scope: str = "alliance",
) -> tuple[EventDefinition, Occurrence]:
    today = date.today()
    event = EventDefinition(
        owning_tenant_id=tenant_id,
        scope=scope,
        name=name,
        interval_days=7,
        start_time_utc=time(19, 0),
        duration_hours=1.0,
        discord_channel="#test",
        description="Test event",
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
        tenant_id=tenant_id,
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

    async def test_community_event_appears(self, client: AsyncClient, db_session: AsyncSession, tenant: dict):
        await _make_event_with_occurrence(db_session, tenant["id"], "Community Event", leadership_only=False)
        r = await client.get(f"/t/{tenant['slug']}/api/events")
        assert r.status_code == 200
        names = [e["event_name"] for e in r.json()]
        assert "Community Event" in names

    async def test_leadership_event_excluded(self, client: AsyncClient, db_session: AsyncSession, tenant: dict):
        await _make_event_with_occurrence(db_session, tenant["id"], "Leadership Event", leadership_only=True)
        r = await client.get(f"/t/{tenant['slug']}/api/events")
        assert r.status_code == 200
        names = [e["event_name"] for e in r.json()]
        assert "Leadership Event" not in names

    async def test_mixed_events_only_community_returned(self, client: AsyncClient, db_session: AsyncSession, tenant: dict):
        await _make_event_with_occurrence(db_session, tenant["id"], "Community Event", leadership_only=False)
        await _make_event_with_occurrence(db_session, tenant["id"], "Leadership Event", leadership_only=True)
        r = await client.get(f"/t/{tenant['slug']}/api/events")
        names = [e["event_name"] for e in r.json()]
        assert "Community Event" in names
        assert "Leadership Event" not in names

    async def test_unknown_tenant_slug_404s(self, client: AsyncClient):
        r = await client.get("/t/nonexistent/api/events")
        assert r.status_code == 404


class TestPublicEventsCrossTenantIsolation:

    async def test_other_tenants_alliance_event_not_visible(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, second_tenant: dict
    ):
        await _make_event_with_occurrence(db_session, tenant["id"], "MOD Only Event", leadership_only=False)
        r = await client.get(f"/t/{second_tenant['slug']}/api/events")
        names = [e["event_name"] for e in r.json()]
        assert "MOD Only Event" not in names

    async def test_kingdom_wide_event_visible_on_both_tenants_feeds(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, second_tenant: dict
    ):
        await _make_event_with_occurrence(
            db_session, tenant["id"], "Kingdom vs Kingdom", leadership_only=False, scope="kingdom-wide"
        )
        r1 = await client.get(f"/t/{tenant['slug']}/api/events")
        r2 = await client.get(f"/t/{second_tenant['slug']}/api/events")
        assert "Kingdom vs Kingdom" in [e["event_name"] for e in r1.json()]
        assert "Kingdom vs Kingdom" in [e["event_name"] for e in r2.json()]


class TestICSFeedExcludesLeadership:

    async def test_community_event_in_ics(self, client: AsyncClient, db_session: AsyncSession, tenant: dict):
        await _make_event_with_occurrence(db_session, tenant["id"], "Community Event", leadership_only=False)
        r = await client.get(f"/t/{tenant['slug']}/ics/events.ics")
        assert r.status_code == 200
        assert "Community Event" in r.text

    async def test_leadership_event_not_in_ics(self, client: AsyncClient, db_session: AsyncSession, tenant: dict):
        await _make_event_with_occurrence(db_session, tenant["id"], "Leadership Event", leadership_only=True)
        r = await client.get(f"/t/{tenant['slug']}/ics/events.ics")
        assert r.status_code == 200
        assert "Leadership Event" not in r.text

    async def test_mixed_events_only_community_in_ics(self, client: AsyncClient, db_session: AsyncSession, tenant: dict):
        await _make_event_with_occurrence(db_session, tenant["id"], "Community Event", leadership_only=False)
        await _make_event_with_occurrence(db_session, tenant["id"], "Leadership Event", leadership_only=True)
        r = await client.get(f"/t/{tenant['slug']}/ics/events.ics")
        assert "Community Event" in r.text
        assert "Leadership Event" not in r.text

    async def test_unknown_tenant_slug_404s(self, client: AsyncClient):
        r = await client.get("/t/nonexistent/ics/events.ics")
        assert r.status_code == 404
