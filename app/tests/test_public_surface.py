"""Unified event model, Phase 3 (spec §66.2, §66.6): the public API, the ICS
feeds and last-activity, all served from the new tables.

The point that matters most is `TestLeadershipEventsAreNeverPublic`: every
public read path is checked for a leadership-only event, so a new public
route that bypasses `services/public_events.public_rows` fails here.
"""
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from models.db import Delivery, Event, EventAlliance, EventOccurrence
from tests.unified_helpers import add_audience, make_event, sync

UTC = timezone.utc
TODAY = date.today()
NOW = datetime.now(UTC)


def _row(rows, name):
    return next(r for r in rows if r["event_name"] == name)


async def _published(sf, tenant, name="Bear Hunt", **kw):
    """An event whose first occurrence is today, with occurrences generated."""
    kw.setdefault("anchor", TODAY)
    kw.setdefault("interval", None)
    kw.setdefault("reminders", ())
    event_id = await make_event(sf, tenant, name=name, **kw)
    await sync(sf, event_id, now=datetime.combine(TODAY, time(0, 0), tzinfo=UTC))
    return event_id


async def _mark_posted(sf, event_id):
    async with sf() as s:
        for d in (await s.execute(
            select(Delivery).join(EventOccurrence, EventOccurrence.id == Delivery.occurrence_id)
            .where(EventOccurrence.event_id == event_id))).scalars():
            d.status, d.posted_at_utc = "posted", NOW
        await s.commit()


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
PUBLIC_JSON = ["/api/events/mod", "/api/events"]
PUBLIC_ICS = ["/events/mod.ics", "/events.ics"]


class TestLeadershipEventsAreNeverPublic:
    @pytest.mark.parametrize("scope", ["alliance", "kingdom-wide"])
    @pytest.mark.parametrize("url", PUBLIC_JSON + PUBLIC_ICS)
    async def test_absent_from_every_public_list(self, client, sf, tenant, second_tenant, url, scope):
        await _published(sf, tenant, "Secret Plan", leadership_only=True, scope=scope)
        await _published(sf, tenant, "Open Day")
        body = (await client.get(url)).text
        assert "Open Day" in body
        assert "Secret Plan" not in body

    async def test_absent_from_other_alliances_view_too(self, client, sf, tenant, second_tenant):
        await _published(sf, tenant, "Secret Plan", leadership_only=True, scope="kingdom-wide")
        assert (await client.get("/api/events/nsr")).json() == []

    @pytest.mark.parametrize("url", ["/api/last-activity/mod", "/api/last-activity"])
    async def test_absent_from_last_activity(self, client, sf, tenant, url):
        event_id = await _published(sf, tenant, "Secret Plan", leadership_only=True)
        await _mark_posted(sf, event_id)
        assert (await client.get(url)).json() is None

    async def test_becoming_public_again_shows_it(self, client, sf, tenant):
        event_id = await _published(sf, tenant, "Was Secret", leadership_only=True)
        async with sf() as s:
            (await s.get(Event, event_id)).leadership_only = False
            await s.commit()
        assert "Was Secret" in (await client.get("/api/events/mod")).text


class TestVisibility:
    async def test_alliance_event_not_visible_to_another_alliance(self, client, sf, tenant, second_tenant):
        await _published(sf, tenant, "MOD Only")
        assert (await client.get("/api/events/nsr")).json() == []
        assert _row((await client.get("/api/events/mod")).json(), "MOD Only")

    async def test_kingdom_wide_event_visible_to_every_alliance(self, client, sf, tenant, second_tenant):
        await _published(sf, tenant, "Everyone", scope="kingdom-wide")
        for slug in ("mod", "nsr"):
            assert _row((await client.get(f"/api/events/{slug}")).json(), "Everyone")

    async def test_subset_audience_visible_to_listed_alliance_only(self, client, sf, tenant, second_tenant):
        await _published(sf, tenant, "Joint", audience=[tenant["id"], second_tenant["id"]])
        assert _row((await client.get("/api/events/nsr")).json(), "Joint")

    async def test_inactive_event_is_hidden(self, client, sf, tenant):
        event_id = await _published(sf, tenant, "Paused")
        async with sf() as s:
            (await s.get(Event, event_id)).active = False
            await s.commit()
        assert (await client.get("/api/events/mod")).json() == []

    async def test_unknown_alliance_is_404(self, client):
        assert (await client.get("/api/events/nope")).status_code == 404
        assert (await client.get("/events/nope.ics")).status_code == 404

    async def test_needs_no_login(self, client_no_session, sf, tenant):
        await _published(sf, tenant, "Open")
        assert (await client_no_session.get("/api/events/mod")).status_code == 200


class TestRowShape:
    async def test_event_row(self, client, sf, tenant):
        await _published(sf, tenant, "Bear Hunt", message="Hello", reminders=(60, 10), duration=2.0)
        row = _row((await client.get("/api/events/mod")).json(), "Bear Hunt")
        assert row["kind"] == "event" and row["duration_hours"] == 2.0
        assert row["type"] == {"name": "General", "color": "#475569"}
        assert row["description"] == "Hello" and row["reminder_minutes"] == [60, 10]
        assert row["notify_minutes_before"] == 60 and row["post_status"] == "pending"
        assert row["end_datetime_utc"] is not None and "tenant_slug" not in row

    async def test_message_without_duration_is_listed_but_not_in_the_calendar_feed(self, client, sf, tenant):
        await _published(sf, tenant, "Reset Notice", duration=None)
        row = _row((await client.get("/api/events/mod")).json(), "Reset Notice")
        assert row["kind"] == "announcement" and row["end_datetime_utc"] is None
        assert "Reset Notice" not in (await client.get("/events/mod.ics")).text

    async def test_posted_status(self, client, sf, tenant):
        event_id = await _published(sf, tenant)
        await _mark_posted(sf, event_id)
        assert _row((await client.get("/api/events/mod")).json(), "Bear Hunt")["post_status"] == "posted"

    async def test_cover_image_is_included(self, client, sf, tenant):
        event_id = await _published(sf, tenant)
        async with sf() as s:
            (await s.get(Event, event_id)).cover_image_data = "data:image/png;base64,AAAA"
            await s.commit()
        assert _row((await client.get("/api/events/mod")).json(), "Bear Hunt")["cover_image_data"].startswith("data:")

    async def test_moved_occurrence_shows_its_own_time(self, client, sf, tenant):
        await _published(sf, tenant)
        moved = datetime.combine(TODAY, time(21, 30), tzinfo=UTC)
        async with sf() as s:
            occ = (await s.execute(select(EventOccurrence))).scalars().first()
            occ.start_datetime_utc_override = moved
            await s.commit()
        row = _row((await client.get("/api/events/mod")).json(), "Bear Hunt")
        assert row["start_datetime_utc"] == moved.isoformat()
        assert row["end_datetime_utc"] == (moved + timedelta(hours=2)).isoformat()

    async def test_occurrence_message_override_wins(self, client, sf, tenant):
        await _published(sf, tenant, message="normal")
        async with sf() as s:
            (await s.execute(select(EventOccurrence))).scalars().first().message_override = "just today"
            await s.commit()
        assert _row((await client.get("/api/events/mod")).json(), "Bear Hunt")["description"] == "just today"

    async def test_alliance_message_override_shows_on_that_alliances_page(self, client, sf, tenant, second_tenant):
        await _published(sf, tenant, message="normal", scope="kingdom-wide")
        async with sf() as s:
            s.add(EventAlliance(event_id=(await s.execute(select(Event.id))).scalar_one(),
                                tenant_id=second_tenant["id"], message_override="for NSR"))
            await s.commit()
        assert _row((await client.get("/api/events/nsr")).json(), "Bear Hunt")["description"] == "for NSR"
        assert _row((await client.get("/api/events/mod")).json(), "Bear Hunt")["description"] == "normal"


class TestCancelledAndHistory:
    async def test_cancelled_occurrence_is_shown_as_cancelled(self, client, sf, tenant):
        await _published(sf, tenant)
        async with sf() as s:
            (await s.execute(select(EventOccurrence))).scalars().first().status = "cancelled"
            await s.commit()
        assert _row((await client.get("/api/events/mod")).json(), "Bear Hunt")["post_status"] == "cancelled"

    async def test_history_left_by_a_split_is_not_public(self, client, sf, tenant):
        """A cancelled occurrence the event's current schedule no longer
        produces is audit history, not something the public should see."""
        event_id = await _published(sf, tenant, interval=7)
        async with sf() as s:
            event = await s.get(Event, event_id)
            event.until_date = TODAY
            for occ in (await s.execute(select(EventOccurrence).where(
                    EventOccurrence.occurrence_date > TODAY))).scalars():
                occ.status = "cancelled"
            await s.commit()
        rows = (await client.get("/api/events/mod")).json()
        assert [r["occurrence_date"] for r in rows] == [str(TODAY)]


class TestCombinedView:
    async def test_alliance_event_has_a_row_per_audience_alliance(self, client, sf, tenant, second_tenant):
        await _published(sf, tenant, "Joint", audience=[tenant["id"], second_tenant["id"]])
        rows = [r for r in (await client.get("/api/events")).json() if r["event_name"] == "Joint"]
        assert sorted(r["tenant_slug"] for r in rows) == ["mod", "nsr"]
        assert {r["tenant_name"] for r in rows} == {"MOD", "NSR"}

    async def test_kingdom_wide_event_is_one_row(self, client, sf, tenant, second_tenant):
        await _published(sf, tenant, "Everyone", scope="kingdom-wide")
        rows = [r for r in (await client.get("/api/events")).json() if r["event_name"] == "Everyone"]
        assert len(rows) == 1 and rows[0]["scope"] == "kingdom-wide"

    async def test_kingdom_wide_row_hides_the_anchor_alliance(self, client, sf, tenant, second_tenant):
        await _published(sf, tenant, "Everyone", scope="kingdom-wide")
        row = next(r for r in (await client.get("/api/events")).json() if r["event_name"] == "Everyone")
        assert not {"tenant_name", "tenant_slug", "tenant_color"} & row.keys()

    async def test_rows_are_ordered_by_start(self, client, sf, tenant):
        await _published(sf, tenant, "Late", start="21:00")
        await _published(sf, tenant, "Early", start="08:00")
        names = [r["event_name"] for r in (await client.get("/api/events")).json()]
        assert names.index("Early") < names.index("Late")


class TestChannelNameResolution:
    async def _fake_channels(self, monkeypatch, channels, error=""):
        monkeypatch.setattr("routers.events._channel_name_cache", {})

        async def fake(token, guild_id):
            return channels, error
        monkeypatch.setattr("routers.events.get_guild_channels", fake)

    async def test_alliance_destination_name_is_resolved(self, client, sf, configured, monkeypatch):
        await self._fake_channels(monkeypatch, [{"id": "chan-mod", "name": "announcements"}])
        await _published(sf, configured)
        assert _row((await client.get("/api/events/mod")).json(), "Bear Hunt")["notification_channel_name"] == "announcements"

    async def test_a_leadership_only_destination_is_never_exposed(self, client, sf, tenant, monkeypatch):
        await self._fake_channels(monkeypatch, [{"id": "lead", "name": "leaders"}, {"id": "pub", "name": "general"}])
        await add_audience(sf, tenant, "lead", label="Leaders", leadership=True)
        await add_audience(sf, tenant, "pub", label="Public")
        await _published(sf, tenant)
        assert _row((await client.get("/api/events/mod")).json(), "Bear Hunt")["notification_channel_name"] == "general"

    async def test_no_destination_is_null(self, client, sf, tenant):
        await _published(sf, tenant)
        assert _row((await client.get("/api/events/mod")).json(), "Bear Hunt")["notification_channel_name"] is None

    async def test_discord_error_degrades_to_null_not_a_500(self, client, sf, configured, monkeypatch):
        await self._fake_channels(monkeypatch, [], "401 Unauthorized")
        await _published(sf, configured)
        r = await client.get("/api/events/mod")
        assert r.status_code == 200 and _row(r.json(), "Bear Hunt")["notification_channel_name"] is None

    async def test_combined_view_resolves_too(self, client, sf, configured, monkeypatch):
        await self._fake_channels(monkeypatch, [{"id": "chan-mod", "name": "general"}])
        await _published(sf, configured)
        assert _row((await client.get("/api/events")).json(), "Bear Hunt")["notification_channel_name"] == "general"


class TestCanonicalUrls:
    """The public URLs are /events and /events/{slug} for pages, /api/events[/{slug}] for data and
    /events.ics and /events/{slug}.ics for calendars (spec §74). The old /t/{slug}/... paths are gone."""

    async def test_alliance_page_and_feed_resolve_to_different_handlers(self, client, tenant):
        page = await client.get("/events/mod")
        feed = await client.get("/events/mod.ics")
        assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
        assert feed.status_code == 200 and feed.headers["content-type"].startswith("text/calendar")

    async def test_unknown_alliance_is_404_on_every_public_path(self, client):
        for path in ("/events/nope", "/events/nope.ics", "/api/events/nope", "/api/last-activity/nope"):
            assert (await client.get(path)).status_code == 404, path

    @pytest.mark.parametrize("path", [
        "/t/mod/events", "/t/mod/api/events", "/t/mod/ics/events.ics", "/t/mod/api/last-activity", "/ics/events.ics",
    ])
    async def test_old_paths_are_gone(self, client, tenant, path):
        assert (await client.get(path)).status_code == 404

    async def test_chip_hrefs_and_feed_urls_in_the_script_match_the_routes(self):
        js = (STATIC_DIR / "events-public.js").read_text(encoding="utf-8")
        for fragment in ("`/events/${encodeURIComponent(TENANT_SLUG)}.ics`", "'/events.ics'", "`/api/events/${encodeURIComponent(TENANT_SLUG)}`"):
            assert fragment in js, fragment


class TestIcsContent:
    async def test_feed_has_the_event(self, client, sf, tenant):
        await _published(sf, tenant, "Bear Hunt", message="Bring bandages")
        r = await client.get("/events/mod.ics")
        assert r.headers["content-type"].startswith("text/calendar")
        assert "SUMMARY:Bear Hunt" in r.text and "DESCRIPTION:Bring bandages" in r.text
        assert "STATUS:TENTATIVE" in r.text

    async def test_feed_header_has_no_nonstandard_properties(self, client, sf, tenant):
        """CALNAME is not an iCalendar property; X-WR-CALNAME is the one calendar apps read."""
        await _published(sf, tenant)
        for path in PUBLIC_ICS:
            lines = (await client.get(path)).text.splitlines()
            assert not any(ln.startswith("CALNAME") for ln in lines)
            assert any(ln.startswith("X-WR-CALNAME:") for ln in lines)

    async def test_feeds_answer_head_for_subscription_probes(self, client, sf, tenant):
        await _published(sf, tenant)
        for path in PUBLIC_ICS:
            r = await client.head(path)
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/calendar")

    async def test_posted_is_confirmed_and_cancelled_is_cancelled(self, client, sf, tenant):
        event_id = await _published(sf, tenant)
        await _mark_posted(sf, event_id)
        assert "STATUS:CONFIRMED" in (await client.get("/events/mod.ics")).text
        async with sf() as s:
            (await s.execute(select(EventOccurrence))).scalars().first().status = "cancelled"
            await s.commit()
        assert "STATUS:CANCELLED" in (await client.get("/events/mod.ics")).text

    async def test_moved_occurrence_uses_the_new_time(self, client, sf, tenant):
        await _published(sf, tenant)
        async with sf() as s:
            (await s.execute(select(EventOccurrence))).scalars().first().start_datetime_utc_override = (
                datetime.combine(TODAY, time(22, 0), tzinfo=UTC))
            await s.commit()
        assert f"DTSTART:{TODAY:%Y%m%d}T220000Z" in (await client.get("/events/mod.ics")).text

    async def test_uids_are_stable_across_requests(self, client, sf, tenant):
        await _published(sf, tenant)
        first = [ln for ln in (await client.get("/events/mod.ics")).text.splitlines() if ln.startswith("UID:")]
        second = [ln for ln in (await client.get("/events/mod.ics")).text.splitlines() if ln.startswith("UID:")]
        assert first == second and len(first) == 1

    async def test_combined_feed_lists_every_alliance(self, client, sf, tenant, second_tenant):
        await _published(sf, tenant, "MOD Thing")
        await _published(sf, second_tenant, "NSR Thing")
        text = (await client.get("/events.ics")).text
        assert "MOD Thing" in text and "NSR Thing" in text


class TestLastActivity:
    async def test_none_before_anything_posted(self, client, sf, tenant):
        await _published(sf, tenant)
        assert (await client.get("/api/last-activity/mod")).json() is None

    async def test_most_recent_posted_delivery(self, client, sf, tenant):
        event_id = await _published(sf, tenant, "Posted One")
        await _mark_posted(sf, event_id)
        body = (await client.get("/api/last-activity/mod")).json()
        assert body["name"] == "Posted One" and body["kind"] == "event"
        combined = (await client.get("/api/last-activity")).json()
        assert combined["tenant_slug"] == "mod"


class TestRoster:
    async def test_alliances_sorted_with_public_fields_only(self, client, tenant, second_tenant):
        body = (await client.get("/api/alliances")).json()
        assert [a["name"] for a in body] == sorted(a["name"] for a in body)
        assert set(body[0]) == {"name", "slug", "color", "icon_image_data"}
