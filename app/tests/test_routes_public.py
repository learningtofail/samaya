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


class TestCombinedPublicViews:
    """The bare /events, /api/events, /ics/events.ics — kept alongside
    the per-alliance pages, not instead of them, per explicit request
    after the multi-tenant split initially dropped the combined view."""

    async def test_combined_api_includes_every_tenant(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, second_tenant: dict
    ):
        await _make_event_with_occurrence(db_session, tenant["id"], "MOD Event", leadership_only=False)
        await _make_event_with_occurrence(db_session, second_tenant["id"], "NSR Event", leadership_only=False)
        r = await client.get("/api/events")
        assert r.status_code == 200
        names = [e["event_name"] for e in r.json()]
        assert "MOD Event" in names
        assert "NSR Event" in names

    async def test_combined_api_excludes_leadership(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict
    ):
        await _make_event_with_occurrence(db_session, tenant["id"], "Leadership Event", leadership_only=True)
        r = await client.get("/api/events")
        names = [e["event_name"] for e in r.json()]
        assert "Leadership Event" not in names

    async def test_combined_api_includes_tenant_name_for_badge(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict
    ):
        await _make_event_with_occurrence(db_session, tenant["id"], "MOD Event", leadership_only=False)
        r = await client.get("/api/events")
        event = next(e for e in r.json() if e["event_name"] == "MOD Event")
        assert event["tenant_slug"] == tenant["slug"]
        assert event["tenant_name"] == "MOD"

    async def test_combined_page_serves_html(self, client: AsyncClient):
        # Skipped, not failing silently: this route (like the pre-existing
        # /t/{slug}/events and /admin) hardcodes the container path
        # /app/static/events.html, which only exists inside the real
        # Docker image — not in a local/CI test run. Same pre-existing
        # limitation as the per-tenant HTML page, which has never had a
        # test for this reason either.
        pytest.skip("Requires the real container filesystem (/app/static/...) — see comment")

    async def test_combined_ics_includes_every_tenant(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, second_tenant: dict
    ):
        await _make_event_with_occurrence(db_session, tenant["id"], "MOD Event", leadership_only=False)
        await _make_event_with_occurrence(db_session, second_tenant["id"], "NSR Event", leadership_only=False)
        r = await client.get("/ics/events.ics")
        assert r.status_code == 200
        assert "MOD Event" in r.text
        assert "NSR Event" in r.text

    async def test_combined_ics_excludes_leadership(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict
    ):
        await _make_event_with_occurrence(db_session, tenant["id"], "Leadership Event", leadership_only=True)
        r = await client.get("/ics/events.ics")
        assert "Leadership Event" not in r.text

    async def test_per_tenant_uid_unchanged_by_combined_feed_addition(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict
    ):
        """Regression guard: the per-tenant feed's UID formula must stay
        byte-identical to what was live before the combined feed existed
        (see routers/ics.py's _build_calendar docstring) — this test
        pins the exact hash so a future refactor can't silently change
        it again without failing here first."""
        import hashlib
        await _make_event_with_occurrence(db_session, tenant["id"], "Weekly Raid", leadership_only=False)
        r = await client.get(f"/t/{tenant['slug']}/ics/events.ics")
        today = date.today()
        expected_base = f"{tenant['slug']}-weekly-raid-{today}"
        expected_uid = hashlib.md5(expected_base.encode()).hexdigest() + "@ks138.taraka.dev"
        assert expected_uid in r.text


class TestAllianceRoster:
    """GET /api/alliances — the public, unauthenticated roster powering the
    alliance switcher on the public events pages (spec §26)."""

    async def test_lists_every_tenant_sorted_by_name(
        self, client: AsyncClient, tenant: dict, second_tenant: dict
    ):
        r = await client.get("/api/alliances")
        assert r.status_code == 200
        body = r.json()
        names = [a["name"] for a in body]
        assert names == sorted(names)
        assert {"name": "MOD", "slug": "mod"}.items() <= body[names.index("MOD")].items()
        assert {"name": "NSR", "slug": "nsr"}.items() <= body[names.index("NSR")].items()

    async def test_each_entry_has_name_slug_color_only(
        self, client: AsyncClient, tenant: dict
    ):
        r = await client.get("/api/alliances")
        body = r.json()
        assert body
        assert set(body[0].keys()) == {"name", "slug", "color", "icon_image_data"}

    async def test_requires_no_auth(self, client: AsyncClient, tenant: dict):
        """Unlike admin's GET /admin/api/tenants, this route needs no
        session cookie — the fixture `client` here is unauthenticated by
        default (auth is added explicitly via test_user/login elsewhere)."""
        r = await client.get("/api/alliances")
        assert r.status_code == 200


class TestPublicNotificationChannelName:
    """Spec §53 — the public events feed resolves an event/announcement's
    notification channel *ID* to a channel *name* server-side, since the
    public page itself has no bot-API access of its own."""

    async def test_event_row_gets_resolved_channel_name(
        self, client: AsyncClient, tenant: dict, db_session: AsyncSession, monkeypatch
    ):
        event, occ = await _make_event_with_occurrence(db_session, tenant["id"], "Weekly Reset", leadership_only=False)
        event.notification_channel_id = "chan-1"
        await db_session.commit()

        monkeypatch.setattr("routers.events._channel_name_cache", {})
        async def fake_channels(token, guild_id):
            return [{"id": "chan-1", "name": "announcements"}], ""
        monkeypatch.setattr("routers.events.get_guild_channels", fake_channels)

        r = await client.get(f"/t/{tenant['slug']}/api/events")
        assert r.status_code == 200
        row = next(row for row in r.json() if row["event_name"] == "Weekly Reset")
        assert row["notification_channel_name"] == "announcements"

    async def test_no_notification_channel_configured_is_none(
        self, client: AsyncClient, tenant: dict, db_session: AsyncSession
    ):
        await _make_event_with_occurrence(db_session, tenant["id"], "No Channel Event", leadership_only=False)
        r = await client.get(f"/t/{tenant['slug']}/api/events")
        row = next(row for row in r.json() if row["event_name"] == "No Channel Event")
        assert row["notification_channel_name"] is None

    async def test_discord_api_error_falls_back_to_none_not_crash(
        self, client: AsyncClient, tenant: dict, db_session: AsyncSession, monkeypatch
    ):
        event, occ = await _make_event_with_occurrence(db_session, tenant["id"], "Errored Channel Event", leadership_only=False)
        event.notification_channel_id = "chan-1"
        await db_session.commit()

        monkeypatch.setattr("routers.events._channel_name_cache", {})
        async def fake_channels(token, guild_id):
            return [], "401 Unauthorized"
        monkeypatch.setattr("routers.events.get_guild_channels", fake_channels)

        r = await client.get(f"/t/{tenant['slug']}/api/events")
        assert r.status_code == 200
        row = next(row for row in r.json() if row["event_name"] == "Errored Channel Event")
        assert row["notification_channel_name"] is None

    async def test_combined_view_also_resolves_channel_name(
        self, client: AsyncClient, tenant: dict, db_session: AsyncSession, monkeypatch
    ):
        event, occ = await _make_event_with_occurrence(db_session, tenant["id"], "Combined Channel Event", leadership_only=False)
        event.notification_channel_id = "chan-9"
        await db_session.commit()

        monkeypatch.setattr("routers.events._channel_name_cache", {})
        async def fake_channels(token, guild_id):
            return [{"id": "chan-9", "name": "general"}], ""
        monkeypatch.setattr("routers.events.get_guild_channels", fake_channels)

        r = await client.get("/api/events")
        row = next(row for row in r.json() if row["event_name"] == "Combined Channel Event")
        assert row["notification_channel_name"] == "general"
