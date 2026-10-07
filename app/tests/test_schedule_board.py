"""Spec §82: the schedule board. Rendering, hash-based edits, recreate and
removal, isolation from reminders, the API and its permissions."""
import json
from datetime import date, datetime, timedelta

from sqlalchemy import select, update

from models.db import AuditLog, AudienceDestination, Event, EventOccurrence
from services.event_engine import run_delivery_tick
from services.schedule_board import (
    BoardLine, ERROR_RETRY_AFTER, MAX_CHARS, escape_markdown, refresh_destination, render_board, run_board_refresh,
)
from tests.unified_helpers import NOW, UTC, add_audience, make_event, sync

A = "/admin/api"
TODAY = date(2026, 10, 1)


def _at(day, hour=19, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


async def _board(sf, channel="chan-mod", scope="kingdom", tenant_id=None, **extra):
    async with sf() as s:
        await s.execute(update(AudienceDestination).where(AudienceDestination.channel_id == channel)
                        .values(board_scope=scope, board_tenant_id=tenant_id, **extra))
        await s.commit()


async def _dest(sf, channel="chan-mod"):
    async with sf() as s:
        return (await s.execute(select(AudienceDestination).where(AudienceDestination.channel_id == channel)
                                .execution_options(populate_existing=True))).scalar_one()


async def _event(sf, tenant, **kw):
    event_id = await make_event(sf, tenant, reminders=(), interval=None, **kw)
    await sync(sf, event_id)
    return event_id


def _board_messages(fake, channel="chan-mod"):
    return [text for (chan, _), text in fake.messages.items() if chan == channel]


class TestRender:
    def test_days_utc_and_relative_time(self):
        text = render_board("Kingdom 138 schedule", [BoardLine(_at(2), "Bear Hunt", ("MOD",))], TODAY)
        assert text.startswith("📅 **Kingdom 138 schedule**\nNext 7 days · times in UTC")
        assert "**Fri 2 Oct**" in text and "`19:00` **Bear Hunt** · MOD · <t:" in text and ":R>" in text
        assert " your time · <t:" in text

    def test_today_is_marked_and_days_are_sorted(self):
        text = render_board("S", [BoardLine(_at(3), "B"), BoardLine(_at(1, 8), "A")], TODAY)
        assert text.index("**A**") < text.index("**B**") and "**Thu 1 Oct** (today)" in text

    def test_cancelled_is_struck_through_without_a_time_stamp(self):
        text = render_board("S", [BoardLine(_at(2), "Bear Hunt", cancelled=True)], TODAY)
        assert "~~Bear Hunt~~ cancelled" in text and "<t:" not in text

    def test_empty_board_says_so(self):
        assert render_board("S", [], TODAY).endswith("Nothing is scheduled.")

    def test_markdown_and_mentions_are_neutralised(self):
        assert escape_markdown("**x** _y_ ~z~ `c` [l]") == r"\*\*x\*\* \_y\_ \~z\~ \`c\` \[l\]"
        assert "@everyone" not in escape_markdown("@everyone") and "@​everyone" in escape_markdown("@everyone")

    def test_a_long_schedule_is_cut_with_a_count_and_stays_under_the_limit(self):
        lines = [BoardLine(_at(2 + i % 5, i % 24, i % 60), f"Event number {i} " + "x" * 40) for i in range(80)]
        text = render_board("S", lines, TODAY)
        assert len(text) <= MAX_CHARS and "…and " in text and "more" in text
        shown = text.count("your time")
        assert f"…and {80 - shown} more" in text


class TestFormat:
    """Spec §82.9: duration, links, the Last edit line."""

    def test_duration_formats(self):
        from services.schedule_board import format_duration
        assert [format_duration(h) for h in (None, 0, 0.75, 1, 1.5, 2, 0.5)] == ["", "", "45 min", "1 h", "1.5 h", "2 h", "30 min"]

    def test_a_line_shows_duration_only_when_the_event_has_one(self):
        with_duration = render_board("S", [BoardLine(_at(2), "Bear Hunt", ("MOD",), going=4, duration_hours=2)], TODAY)
        assert "`19:00` **Bear Hunt** · MOD · 4 in · 2 h · <t:" in with_duration
        without = render_board("S", [BoardLine(_at(2), "Announcement")], TODAY)
        assert " h · " not in without and " min · " not in without

    def test_last_edit_line_is_second_and_uses_discord_timestamps(self):
        text = render_board("S", [], TODAY, updated=NOW)
        stamp = int(NOW.timestamp())
        assert text.splitlines()[1] == f"Last edit: <t:{stamp}:f> (<t:{stamp}:R>)"
        assert "Last edit" not in render_board("S", [], TODAY)

    def test_links_are_a_footer_without_embeds(self):
        links = [("Full schedule", "https://x.example/events"), ("MOD", "https://x.example/events/mod")]
        text = render_board("S", [BoardLine(_at(2), "B")], TODAY, links)
        assert text.splitlines()[-1] == "🔗 [Full schedule](<https://x.example/events>) · [MOD](<https://x.example/events/mod>)"
        assert render_board("S", [], TODAY, links).endswith("(<https://x.example/events/mod>)")

    def test_a_long_schedule_keeps_the_footer(self):
        links = [("Full schedule", "https://x.example/events")]
        lines = [BoardLine(_at(2 + i % 5, i % 24, i % 60), f"Event number {i} " + "x" * 40) for i in range(80)]
        text = render_board("S", lines, TODAY, links, updated=NOW)
        assert len(text) <= MAX_CHARS and text.splitlines()[-1].startswith("🔗 [Full schedule]") and "…and " in text


class TestHeartbeatAndLinks:
    """Spec §82.9 through the engine."""

    async def test_unchanged_board_is_edited_only_after_the_heartbeat(self, sf, fake, configured):
        from services.schedule_board import HEARTBEAT_AFTER
        await _event(sf, configured, name="Bear Hunt")
        await _board(sf)
        await run_board_refresh(sf, fake, NOW)
        calls = len(fake.calls)
        assert await run_board_refresh(sf, fake, NOW + HEARTBEAT_AFTER - timedelta(seconds=1)) == {"unchanged": 1}
        assert len(fake.calls) == calls
        later = NOW + HEARTBEAT_AFTER
        assert await run_board_refresh(sf, fake, later) == {"edited": 1}
        assert f"<t:{int(later.timestamp())}:f>" in _board_messages(fake)[0]
        assert fake.count("send") == 1  # edited in place, never reposted
        assert await run_board_refresh(sf, fake, later + timedelta(minutes=1)) == {"unchanged": 1}

    async def test_the_last_edit_line_does_not_count_as_a_change(self, sf, fake, configured):
        await _event(sf, configured, name="Bear Hunt")
        await _board(sf)
        await run_board_refresh(sf, fake, NOW)
        first_hash = (await _dest(sf)).board_hash
        await run_board_refresh(sf, fake, NOW + timedelta(minutes=20))
        assert (await _dest(sf)).board_hash == first_hash

    async def test_a_real_change_still_shows_within_the_minute(self, sf, fake, configured):
        event_id = await _event(sf, configured, name="Bear Hunt")
        await _board(sf)
        await run_board_refresh(sf, fake, NOW)
        async with sf() as s:
            (await s.get(Event, event_id)).name = "Renamed"
            await s.commit()
        assert await run_board_refresh(sf, fake, NOW + timedelta(minutes=1)) == {"edited": 1}

    async def test_kingdom_board_links_the_combined_page_and_alliances_with_events(self, sf, fake, configured, second_tenant, monkeypatch):
        monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test/")
        await _event(sf, configured, name="MOD Hunt")
        await _board(sf)
        await run_board_refresh(sf, fake, NOW)
        footer = _board_messages(fake)[0].splitlines()[-1]
        assert "[Full schedule](<https://example.test/events>)" in footer
        assert "[MOD](<https://example.test/events/mod>)" in footer and "nsr" not in footer

    async def test_alliance_board_links_its_own_page_and_default_base_is_production(self, sf, fake, configured, second_tenant, monkeypatch):
        monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
        await _event(sf, configured, name="MOD Hunt")
        await _board(sf, scope="alliance", tenant_id=configured["id"])
        await run_board_refresh(sf, fake, NOW)
        assert _board_messages(fake)[0].splitlines()[-1] == "🔗 [MOD schedule](<https://ks138.taraka.dev/events/mod>)"

    async def test_duration_comes_from_the_event(self, sf, fake, configured):
        await _event(sf, configured, name="Bear Hunt", duration=1.5)
        await _event(sf, configured, name="Notice", duration=None, start="20:00")
        await _board(sf)
        await run_board_refresh(sf, fake, NOW)
        text = _board_messages(fake)[0]
        bear = next(line for line in text.splitlines() if "**Bear Hunt**" in line)
        notice = next(line for line in text.splitlines() if "**Notice**" in line)
        assert " · 1.5 h · " in bear and " h · " not in notice and " min · " not in notice


class TestRefresh:
    async def test_first_refresh_posts_one_message(self, sf, fake, configured):
        await _event(sf, configured, name="Bear Hunt")
        await _board(sf)
        assert await run_board_refresh(sf, fake, NOW) == {"posted": 1}
        [text] = _board_messages(fake)
        assert "Bear Hunt" in text and "Kingdom 138 schedule" in text
        dest = await _dest(sf)
        assert dest.board_message_id and dest.board_hash and dest.board_error is None

    async def test_nothing_changed_means_no_discord_call(self, sf, fake, configured):
        await _event(sf, configured)
        await _board(sf)
        await run_board_refresh(sf, fake, NOW)
        calls = len(fake.calls)
        assert await run_board_refresh(sf, fake, NOW + timedelta(minutes=1)) == {"unchanged": 1}
        assert len(fake.calls) == calls

    async def test_an_event_change_edits_the_same_message(self, sf, fake, configured):
        event_id = await _event(sf, configured, name="Bear Hunt")
        await _board(sf)
        await run_board_refresh(sf, fake, NOW)
        first_id = (await _dest(sf)).board_message_id
        async with sf() as s:
            (await s.get(Event, event_id)).name = "Bear Hunt Deluxe"
            await s.commit()
        assert await run_board_refresh(sf, fake, NOW + timedelta(minutes=1)) == {"edited": 1}
        assert (await _dest(sf)).board_message_id == first_id
        assert "Bear Hunt Deluxe" in _board_messages(fake)[0] and fake.count("send") == 1

    async def test_cancelling_an_occurrence_shows_on_the_board(self, sf, fake, configured):
        event_id = await _event(sf, configured, name="Bear Hunt")
        await _board(sf)
        await run_board_refresh(sf, fake, NOW)
        async with sf() as s:
            occ = (await s.execute(select(EventOccurrence).where(EventOccurrence.event_id == event_id))).scalars().first()
            occ.status = "cancelled"
            await s.commit()
        await run_board_refresh(sf, fake, NOW + timedelta(minutes=1))
        assert "~~Bear Hunt~~" in _board_messages(fake)[0]

    async def test_midnight_utc_moves_the_window(self, sf, fake, configured):
        await _event(sf, configured, name="Bear Hunt")
        await _board(sf)
        await run_board_refresh(sf, fake, NOW)
        assert "(today)" not in _board_messages(fake)[0]
        assert await run_board_refresh(sf, fake, NOW + timedelta(days=1)) == {"edited": 1}
        assert "**Fri 2 Oct** (today)" in _board_messages(fake)[0]

    async def test_a_deleted_message_is_posted_again(self, sf, fake, configured):
        await _event(sf, configured)
        await _board(sf)
        await run_board_refresh(sf, fake, NOW)
        old_id = (await _dest(sf)).board_message_id
        fake.messages.clear()
        assert await refresh_destination(sf, fake, (await _dest(sf)).id, NOW + timedelta(minutes=1), force=True) == "recreated"
        assert (await _dest(sf)).board_message_id != old_id and len(_board_messages(fake)) == 1

    async def test_turning_the_board_off_deletes_the_message(self, sf, fake, configured):
        await _event(sf, configured)
        await _board(sf)
        await run_board_refresh(sf, fake, NOW)
        await _board(sf, scope=None)
        assert await run_board_refresh(sf, fake, NOW + timedelta(minutes=1)) == {"removed": 1}
        dest = await _dest(sf)
        assert dest.board_message_id is None and dest.board_hash is None and fake.messages == {}
        assert await run_board_refresh(sf, fake, NOW + timedelta(minutes=2)) == {}

    async def test_a_failed_removal_keeps_the_id_and_says_why(self, sf, fake, configured):
        await _event(sf, configured)
        await _board(sf)
        await run_board_refresh(sf, fake, NOW)
        await _board(sf, scope=None)
        fake.delete_error = "403 Missing permissions"
        assert await run_board_refresh(sf, fake, NOW + timedelta(minutes=1)) == {"error": 1}
        dest = await _dest(sf)
        assert dest.board_message_id and "403" in dest.board_error


class TestScope:
    async def test_a_leadership_only_event_is_never_on_a_board(self, sf, fake, configured):
        await _event(sf, configured, name="Public Hunt")
        await _event(sf, configured, name="Secret War Room", leadership_only=True)
        await _board(sf)
        await run_board_refresh(sf, fake, NOW)
        text = _board_messages(fake)[0]
        assert "Public Hunt" in text and "Secret War Room" not in text

    async def test_an_inactive_event_is_not_shown(self, sf, fake, configured):
        await _event(sf, configured, name="Old Hunt", active=False)
        await _board(sf)
        await run_board_refresh(sf, fake, NOW)
        assert "Old Hunt" not in _board_messages(fake)[0]

    async def test_alliance_board_shows_only_that_alliances_schedule(self, sf, fake, configured, second_tenant):
        await _event(sf, configured, name="MOD Hunt")
        await _event(sf, second_tenant, name="NSR Hunt")
        await _board(sf, scope="alliance", tenant_id=configured["id"])
        await run_board_refresh(sf, fake, NOW)
        text = _board_messages(fake)[0]
        assert "MOD schedule" in text and "MOD Hunt" in text and "NSR Hunt" not in text and "· MOD" not in text

    async def test_kingdom_board_lists_each_event_once_with_its_alliances(self, sf, fake, configured, second_tenant):
        await _event(sf, configured, name="Shared Hunt", audience=[configured["id"], second_tenant["id"]])
        await _event(sf, configured, name="Everyone Event", scope="kingdom-wide")
        await _board(sf)
        await run_board_refresh(sf, fake, NOW)
        text = _board_messages(fake)[0]
        assert text.count("Shared Hunt") == 1 and "· MOD, NSR" in text
        assert text.count("Everyone Event") == 1

    async def test_events_outside_the_seven_days_are_left_out(self, sf, fake, configured):
        await _event(sf, configured, name="Far Hunt", anchor=date(2026, 10, 20))
        await _board(sf)
        await run_board_refresh(sf, fake, NOW)
        assert "Far Hunt" not in _board_messages(fake)[0] and "Nothing is scheduled" in _board_messages(fake)[0]

    async def test_another_kingdoms_events_stay_off(self, sf, fake, configured, db_engine):
        from models.db import DiscordServer, Kingdom, Tenant
        async with sf() as s:
            k = Kingdom(name="Elsewhere", slug="else")
            s.add(k)
            await s.flush()
            srv = DiscordServer(kingdom_id=k.id, name="X", guild_id="gx", bot_token="t")
            s.add(srv)
            await s.flush()
            other = Tenant(kingdom_id=k.id, server_id=srv.id, name="Far", slug="far")
            s.add(other)
            await s.commit()
            other_row = {"id": other.id, "slug": "far", "kingdom_id": k.id, "server_id": srv.id}
        await _event(sf, other_row, name="Foreign Hunt")
        await _event(sf, configured, name="Home Hunt")
        await _board(sf)
        await run_board_refresh(sf, fake, NOW)
        text = _board_messages(fake)[0]
        assert "Home Hunt" in text and "Foreign Hunt" not in text

    async def test_a_removed_alliance_is_an_error_not_a_crash(self, sf, fake, configured):
        await _board(sf, scope="alliance", tenant_id=None)
        assert await run_board_refresh(sf, fake, NOW) == {"error": 1}
        assert "removed" in (await _dest(sf)).board_error and fake.messages == {}


class TestFailures:
    async def test_a_post_failure_is_stored_and_never_raises(self, sf, fake, configured):
        await _event(sf, configured)
        await _board(sf)
        fake.post_error = "403 Missing permissions"
        assert await run_board_refresh(sf, fake, NOW) == {"error": 1}
        dest = await _dest(sf)
        assert dest.board_error.startswith("403") and dest.board_message_id is None

    async def test_after_an_error_it_waits_before_trying_again(self, sf, fake, configured):
        await _event(sf, configured)
        await _board(sf)
        fake.post_error = "403 Missing permissions"
        await run_board_refresh(sf, fake, NOW)
        calls = len(fake.calls)
        assert await run_board_refresh(sf, fake, NOW + timedelta(minutes=1)) == {"skipped": 1}
        assert len(fake.calls) == calls
        fake.post_error = ""
        later = NOW + ERROR_RETRY_AFTER + timedelta(seconds=1)
        assert await run_board_refresh(sf, fake, later) == {"posted": 1}
        assert (await _dest(sf)).board_error is None

    async def test_an_edit_failure_keeps_the_message_and_the_old_hash(self, sf, fake, configured):
        event_id = await _event(sf, configured, name="Bear Hunt")
        await _board(sf)
        await run_board_refresh(sf, fake, NOW)
        old = await _dest(sf)
        async with sf() as s:
            (await s.get(Event, event_id)).name = "Renamed"
            await s.commit()
        fake.edit_error = "429 Rate limited"
        assert await run_board_refresh(sf, fake, NOW + timedelta(minutes=1)) == {"error": 1}
        dest = await _dest(sf)
        assert dest.board_message_id == old.board_message_id and dest.board_hash == old.board_hash

    async def test_a_paused_destination_is_skipped(self, sf, fake, configured):
        await _event(sf, configured)
        await _board(sf, paused_at=NOW)
        assert await run_board_refresh(sf, fake, NOW) == {"skipped": 1}
        assert fake.calls == []

    async def test_a_broken_board_does_not_stop_reminders_or_other_boards(self, sf, fake, configured, second_tenant):
        await add_audience(sf, second_tenant, "chan-nsr")
        event_id = await make_event(sf, configured, interval=None, duration=None)
        await sync(sf, event_id)
        await _board(sf)
        await _board(sf, channel="chan-nsr")
        fake.raise_on_send_to = {"chan-mod"}
        counts = await run_board_refresh(sf, fake, _at(2, 18, 0))
        assert counts == {"error": 1, "posted": 1}
        fake.raise_on_send_to = set()
        await run_delivery_tick(sf, fake, _at(2, 18, 0) + timedelta(seconds=30))
        assert any(c[0] == "send" and "Bear Hunt" in c[2] and c[1] == "chan-mod" for c in fake.calls)

    async def test_board_mentions_are_never_enabled_for_pings(self, sf, configured):
        class Spy:
            def __init__(self):
                self.flags = []

            async def post_channel_message(self, token, channel_id, content, *, no_mentions=False):
                self.flags.append(no_mentions)
                return "m1", ""
        await _event(sf, configured, name="@everyone Hunt")
        await _board(sf)
        spy = Spy()
        await run_board_refresh(sf, spy, NOW)
        assert spy.flags == [True]


class TestApi:
    async def _audience(self, client, tenant, **dest):
        body = {"label": "Notifications", "leadership_only": False,
                "destinations": [{"server_id": tenant["server_id"], "channel_id": "555", **dest}],
                "links": [{"tenant_id": tenant["id"], "post_by_default": True}]}
        return await client.post(f"{A}/audiences", json=body)

    async def test_create_with_a_kingdom_board(self, client, tenant):
        resp = await self._audience(client, tenant, board_scope="kingdom")
        assert resp.status_code == 201
        dest = resp.json()["destinations"][0]
        assert dest["board_scope"] == "kingdom" and dest["board_tenant_id"] is None and dest["board_posted"] is False

    async def test_create_with_an_alliance_board(self, client, tenant):
        resp = await self._audience(client, tenant, board_scope="alliance", board_tenant_id=tenant["id"])
        assert resp.status_code == 201 and resp.json()["destinations"][0]["board_tenant_id"] == tenant["id"]

    async def test_validation(self, client, tenant, second_tenant):
        assert (await self._audience(client, tenant, board_scope="alliance")).status_code == 422
        assert (await self._audience(client, tenant, board_scope="kingdom", board_tenant_id=tenant["id"])).status_code == 422
        assert (await self._audience(client, tenant, board_scope="galaxy")).status_code == 422
        assert (await self._audience(client, tenant, board_scope="alliance", board_tenant_id=99999)).status_code == 422

    async def test_alliance_of_another_kingdom_is_rejected(self, client, tenant, sf):
        from models.db import DiscordServer, Kingdom, Tenant
        async with sf() as s:
            k = Kingdom(name="Elsewhere", slug="else2")
            s.add(k)
            await s.flush()
            srv = DiscordServer(kingdom_id=k.id, name="X", guild_id="gx2")
            s.add(srv)
            await s.flush()
            other = Tenant(kingdom_id=k.id, server_id=srv.id, name="Far", slug="far2")
            s.add(other)
            await s.commit()
            other_id = other.id
        resp = await self._audience(client, tenant, board_scope="alliance", board_tenant_id=other_id)
        assert resp.status_code == 422 and "this Kingdom" in resp.json()["detail"]

    async def test_changing_the_scope_keeps_the_message_and_forces_a_render(self, client, tenant, sf, fake):
        created = (await self._audience(client, tenant, board_scope="kingdom")).json()
        async with sf() as s:
            await s.execute(update(AudienceDestination).values(board_message_id="m9", board_hash="h", board_error="old"))
            await s.commit()
        body = {"destinations": [{"server_id": tenant["server_id"], "channel_id": "555",
                                  "board_scope": "alliance", "board_tenant_id": tenant["id"]}]}
        assert (await client.patch(f"{A}/audiences/{created['id']}", json=body)).status_code == 200
        dest = await _dest(sf, "555")
        assert (dest.board_message_id, dest.board_hash, dest.board_error, dest.board_scope) == ("m9", None, None, "alliance")

    async def test_unchanged_scope_keeps_the_hash(self, client, tenant, sf):
        created = (await self._audience(client, tenant, board_scope="kingdom")).json()
        async with sf() as s:
            await s.execute(update(AudienceDestination).values(board_message_id="m9", board_hash="h"))
            await s.commit()
        body = {"label": "Renamed", "destinations": [{"server_id": tenant["server_id"], "channel_id": "555", "board_scope": "kingdom"}]}
        await client.patch(f"{A}/audiences/{created['id']}", json=body)
        assert (await _dest(sf, "555")).board_hash == "h"

    async def test_refresh_now_posts_and_is_audited(self, client, tenant, sf, fake):
        from main import app
        from services.discord_client import get_discord
        app.dependency_overrides[get_discord] = lambda: fake
        created = (await self._audience(client, tenant, board_scope="kingdom")).json()
        dest_id = created["destinations"][0]["id"]
        resp = await client.post(f"{A}/audience-destinations/{dest_id}/board/refresh")
        assert resp.status_code == 200 and resp.json()["outcome"] == "posted" and resp.json()["board_posted"] is True
        assert len(_board_messages(fake, "555")) == 1
        async with sf() as s:
            rows = (await s.execute(select(AuditLog).where(AuditLog.table_name == "audience_destinations"))).scalars().all()
        assert any(json.loads(r.after or "{}").get("board_refresh") == "posted" for r in rows)

    async def test_refresh_reports_a_failure_in_the_answer(self, client, tenant, fake):
        from main import app
        from services.discord_client import get_discord
        app.dependency_overrides[get_discord] = lambda: fake
        fake.post_error = "403 Missing permissions"
        created = (await self._audience(client, tenant, board_scope="kingdom")).json()
        resp = await client.post(f"{A}/audience-destinations/{created['destinations'][0]['id']}/board/refresh")
        assert resp.status_code == 200 and resp.json()["outcome"] == "error" and resp.json()["board_error"].startswith("403")

    async def test_refresh_without_a_board_is_409(self, client, tenant, fake):
        from main import app
        from services.discord_client import get_discord
        app.dependency_overrides[get_discord] = lambda: fake
        created = (await self._audience(client, tenant)).json()
        resp = await client.post(f"{A}/audience-destinations/{created['destinations'][0]['id']}/board/refresh")
        assert resp.status_code == 409

    async def test_refresh_unknown_destination_is_404(self, client, tenant, fake):
        from main import app
        from services.discord_client import get_discord
        app.dependency_overrides[get_discord] = lambda: fake
        assert (await client.post(f"{A}/audience-destinations/99999/board/refresh")).status_code == 404

    async def test_viewers_and_non_coordinators_cannot_refresh_or_set_boards(self, client, make_user_and_client, tenant, fake):
        created = (await self._audience(client, tenant, board_scope="kingdom")).json()
        dest_id = created["destinations"][0]["id"]
        viewer, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")], kingdom_grants=[tenant["kingdom_id"]])
        viewer.headers["X-Tenant-Slug"] = tenant["slug"]
        assert (await viewer.post(f"{A}/audience-destinations/{dest_id}/board/refresh")).status_code == 403
        owner, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "owner")], kingdom_grants=[], discord_id="owner-2")
        owner.headers["X-Tenant-Slug"] = tenant["slug"]
        assert (await owner.post(f"{A}/audience-destinations/{dest_id}/board/refresh")).status_code == 403
        body = {"destinations": [{"server_id": tenant["server_id"], "channel_id": "555", "board_scope": None}]}
        assert (await owner.patch(f"{A}/audiences/{created['id']}", json=body)).status_code == 403

    async def test_public_routes_never_mention_boards(self, client_no_session, sf, configured):
        await _board(sf)
        for path in ("/api/events", "/api/alliances", "/api/kingdom-branding", "/events"):
            body = (await client_no_session.get(path)).text
            assert "board_" not in body and "chan-mod" not in body
