"""Tests for GET /admin/api/occurrences (routers/admin/occurrences.py's
list_occurrences). Had zero test coverage before this file — found
broken (a lazy-load MissingGreenlet crash accessing occ.event without
eager loading) while render-testing the Dashboard view against a real
running server with genuinely fresh queries; the existing test suite's
db_session fixture pattern never exercised this code path in a way
that triggered it, since objects created and queried within the same
test often already have relationships populated in the identity map.
"""
from datetime import date, datetime, time, timedelta, timezone

from httpx import AsyncClient

from models.db import EventDefinition, Occurrence


async def _make_occurrence(db_session, tenant_id: int, name: str, scope: str = "alliance"):
    today = date.today()
    event = EventDefinition(
        owning_tenant_id=tenant_id, scope=scope, name=name,
        interval_days=7, start_time_utc=time(19, 0), duration_hours=1.0,
        anchor_date=today, notification_channel_id="", notification_role_id="",
    )
    db_session.add(event)
    await db_session.commit()
    await db_session.refresh(event)

    start = datetime.combine(today, time(19, 0), tzinfo=timezone.utc)
    occ = Occurrence(
        event_id=event.id, tenant_id=tenant_id, occurrence_date=today,
        start_datetime_utc=start, end_datetime_utc=start + timedelta(hours=1),
        post_status="pending",
    )
    db_session.add(occ)
    await db_session.commit()
    return event, occ


class TestListOccurrences:

    async def test_returns_this_tenants_occurrences(self, client: AsyncClient, db_session, tenant: dict):
        await _make_occurrence(db_session, tenant["id"], "Weekly Raid")
        r = await client.get("/admin/api/occurrences")
        assert r.status_code == 200, r.text
        assert len(r.json()) == 1
        assert r.json()[0]["event_name"] == "Weekly Raid"

    async def test_multiple_occurrences_all_returned(
        self, client: AsyncClient, db_session, tenant: dict
    ):
        """The specific case that crashed: more than one Occurrence, each
        needing its own EventDefinition looked up via the relationship."""
        await _make_occurrence(db_session, tenant["id"], "Weekly Raid")
        await _make_occurrence(db_session, tenant["id"], "Practice Night")
        r = await client.get("/admin/api/occurrences")
        assert r.status_code == 200, r.text
        names = {o["event_name"] for o in r.json()}
        assert names == {"Weekly Raid", "Practice Night"}

    async def test_includes_kingdom_wide_from_other_tenant(
        self, client: AsyncClient, db_session, tenant: dict, second_tenant: dict
    ):
        await _make_occurrence(db_session, tenant["id"], "Kingdom vs Kingdom", scope="kingdom-wide")
        r = await client.get("/admin/api/occurrences", headers={"X-Tenant-Slug": second_tenant["slug"]})
        assert r.status_code == 200, r.text
        assert "Kingdom vs Kingdom" in [o["event_name"] for o in r.json()]

    async def test_excludes_other_tenants_alliance_occurrence(
        self, client: AsyncClient, db_session, tenant: dict, second_tenant: dict
    ):
        await _make_occurrence(db_session, tenant["id"], "MOD Only Event", scope="alliance")
        r = await client.get("/admin/api/occurrences", headers={"X-Tenant-Slug": second_tenant["slug"]})
        assert "MOD Only Event" not in [o["event_name"] for o in r.json()]

    async def test_requires_login(self, client_no_session: AsyncClient, tenant: dict):
        r = await client_no_session.get("/admin/api/occurrences", headers={"X-Tenant-Slug": tenant["slug"]})
        assert r.status_code == 401
