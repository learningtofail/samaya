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

from models.db import Announcement, AnnouncementTarget, EventDefinition, EventTarget, Occurrence


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


async def _make_announcement(
    db_session: AsyncSession,
    owning_tenant_id: int,
    title: str,
    scope: str = "alliance",
    status: str = "scheduled",
) -> Announcement:
    a = Announcement(
        owning_tenant_id=owning_tenant_id,
        scope=scope,
        title=title,
        body_markdown="Test body",
        scheduled_for=datetime.now(timezone.utc) + timedelta(hours=2),
        status=status,
        leadership_only=False,
    )
    db_session.add(a)
    await db_session.commit()
    await db_session.refresh(a)
    return a


class TestSpec61VisibilitySeparateFromNotificationTargets:
    """Spec §61 — visibility/badging on the public pages is driven by
    scope (kingdom-wide vs alliance) and explicit targets alone, never by
    whether a notification channel/role happens to be configured for a
    given destination. See routers/events.py's own comments on the
    per-tenant and combined queries for the reasoning."""

    async def test_announcement_scope_field_round_trips_on_public_api(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict
    ):
        await _make_announcement(db_session, tenant["id"], "Kingdom Reset", scope="kingdom-wide")
        r = await client.get(f"/t/{tenant['slug']}/api/events")
        row = next(row for row in r.json() if row["event_name"] == "Kingdom Reset")
        assert row["scope"] == "kingdom-wide"

    async def test_kingdom_wide_announcement_visible_on_every_same_kingdom_tenant_with_no_targets(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, second_tenant: dict
    ):
        """The actual bug: before §61 an announcement's visibility was
        driven purely by AnnouncementTarget rows, so a kingdom-wide
        announcement with zero explicit targets for a sibling tenant
        never showed up on that tenant's own page at all."""
        await _make_announcement(db_session, tenant["id"], "Kingdom Reset", scope="kingdom-wide")
        r1 = await client.get(f"/t/{tenant['slug']}/api/events")
        r2 = await client.get(f"/t/{second_tenant['slug']}/api/events")
        assert "Kingdom Reset" in [e["event_name"] for e in r1.json()]
        assert "Kingdom Reset" in [e["event_name"] for e in r2.json()]

    async def test_alliance_scope_announcement_not_visible_on_other_tenant_without_a_target(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, second_tenant: dict
    ):
        await _make_announcement(db_session, tenant["id"], "MOD Only Announcement", scope="alliance")
        r = await client.get(f"/t/{second_tenant['slug']}/api/events")
        assert "MOD Only Announcement" not in [e["event_name"] for e in r.json()]

    async def test_kingdom_wide_announcement_single_row_no_alliance_fields_on_combined_view(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, second_tenant: dict
    ):
        """A kingdom-wide item must not fan out into one row per Kingdom
        member on the combined page — that's the whole point of pulling
        scope out of the AnnouncementTarget-driven fan-out. Client-side,
        the absence of tenant_slug/targets is what keeps tenantBadge()
        from rendering any alliance badge at all for it."""
        await _make_announcement(db_session, tenant["id"], "Kingdom Reset", scope="kingdom-wide")
        r = await client.get("/api/events")
        rows = [e for e in r.json() if e["event_name"] == "Kingdom Reset"]
        assert len(rows) == 1
        assert rows[0].get("tenant_slug") is None

    async def test_kingdom_wide_announcement_with_explicit_targets_not_double_counted(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, second_tenant: dict
    ):
        """A kingdom-wide announcement that also happens to carry explicit
        AnnouncementTargets (e.g. for delivery/channel routing) must still
        show as exactly one row on the combined view, not one plus a
        second fan-out row from the target-based query."""
        a = await _make_announcement(db_session, tenant["id"], "Kingdom Reset", scope="kingdom-wide")
        db_session.add(AnnouncementTarget(announcement_id=a.id, tenant_id=tenant["id"], discord_channel_id="chan-1"))
        await db_session.commit()
        r = await client.get("/api/events")
        rows = [e for e in r.json() if e["event_name"] == "Kingdom Reset"]
        assert len(rows) == 1

    async def test_alliance_scope_announcement_with_targets_gets_one_badge_row_per_target(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, second_tenant: dict
    ):
        a = await _make_announcement(db_session, tenant["id"], "Joint Op Announcement", scope="alliance")
        db_session.add(AnnouncementTarget(announcement_id=a.id, tenant_id=tenant["id"], discord_channel_id="chan-1"))
        db_session.add(AnnouncementTarget(announcement_id=a.id, tenant_id=second_tenant["id"], discord_channel_id="chan-2"))
        await db_session.commit()
        r = await client.get("/api/events")
        rows = [e for e in r.json() if e["event_name"] == "Joint Op Announcement"]
        assert {row["tenant_slug"] for row in rows} == {tenant["slug"], second_tenant["slug"]}

        r2 = await client.get(f"/t/{second_tenant['slug']}/api/events")
        assert "Joint Op Announcement" in [e["event_name"] for e in r2.json()]

    async def test_event_target_makes_alliance_scope_event_visible_on_targeted_tenant(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, second_tenant: dict
    ):
        """Bullet 2 of the request: an explicit EventTarget alone — with
        no notification channel/role configured — must be enough to make
        an alliance-scope event show up (and badge) on the targeted
        tenant's own page and the combined page, independent of whether
        a ping channel was ever set for that destination."""
        event, occ = await _make_event_with_occurrence(db_session, tenant["id"], "Cross-Alliance Rally", leadership_only=False)
        db_session.add(EventTarget(event_id=event.id, tenant_id=second_tenant["id"]))
        await db_session.commit()

        r = await client.get(f"/t/{second_tenant['slug']}/api/events")
        assert "Cross-Alliance Rally" in [e["event_name"] for e in r.json()]

        r2 = await client.get("/api/events")
        rows = [e for e in r2.json() if e["event_name"] == "Cross-Alliance Rally"]
        assert {row["tenant_slug"] for row in rows} == {tenant["slug"], second_tenant["slug"]}

    async def test_kingdom_wide_event_with_event_target_not_double_counted(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, second_tenant: dict
    ):
        event, occ = await _make_event_with_occurrence(
            db_session, tenant["id"], "Kingdom Siege", leadership_only=False, scope="kingdom-wide"
        )
        db_session.add(EventTarget(event_id=event.id, tenant_id=second_tenant["id"]))
        await db_session.commit()

        r = await client.get("/api/events")
        rows = [e for e in r.json() if e["event_name"] == "Kingdom Siege"]
        assert len(rows) == 1
        assert rows[0]["scope"] == "kingdom-wide"


class TestPublicEventCoverImage:
    """Spec §35/§62 — cover_image_data was already stored on EventDefinition
    and already read by this page's own Discord-preview modal, but
    _event_row_dict() never actually included it in the response, so the
    preview's cover image silently never rendered here."""

    async def test_event_row_carries_cover_image_data(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict
    ):
        event, occ = await _make_event_with_occurrence(db_session, tenant["id"], "Cover Image Event", leadership_only=False)
        event.cover_image_data = "data:image/png;base64,aGVsbG8="
        await db_session.commit()

        r = await client.get(f"/t/{tenant['slug']}/api/events")
        row = next(e for e in r.json() if e["event_name"] == "Cover Image Event")
        assert row["cover_image_data"] == "data:image/png;base64,aGVsbG8="

    async def test_event_row_cover_image_null_when_unset(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict
    ):
        await _make_event_with_occurrence(db_session, tenant["id"], "No Cover Event", leadership_only=False)
        r = await client.get(f"/t/{tenant['slug']}/api/events")
        row = next(e for e in r.json() if e["event_name"] == "No Cover Event")
        assert row["cover_image_data"] is None
