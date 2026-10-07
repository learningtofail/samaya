"""Spec §87: Notify me (role toggle with recorded intent) and I'm in (attendance for one occurrence).
Pure helpers, clicks through the interaction handler, the reminder buttons and headcount, the board, retention."""
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from models.db import (
    Delivery, Event, EventOccurrence, EventSubscription, OccurrenceRsvp, SignupGroup, SignupRole,
)
from services import rate_limit, signup
from services.discord_commands import handle_interaction_ex
from services.event_engine import run_delivery_tick
from services.schedule_board import board_lines, render_board
from services.signup import (
    DENIED_PERMISSIONS, MSG_CANCELLED, MSG_INACTIVE, MSG_STARTED, button_row, count_line, parse_rsvp, parse_rsvp_switch,
    parse_sub, parse_switch, prune_rsvps, rsvp_custom_id, rsvp_hash, rsvp_switch_custom_id, run_rsvp_refresh,
    sub_custom_id, sub_hash, switch_custom_id, unsafe_role_reason, week_start, window_range, with_count,
)
from tests.unified_helpers import add_audience, make_event, sync

UTC = timezone.utc
GUILD = "test-guild-mod"
CHAN = "chan-mod"
USER, OTHER = "123456789012345678", "987654321098765432"
DAY = date(2026, 10, 2)  # a Friday
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
AT_REMINDER = datetime(2026, 10, 2, 18, 0, 30, tzinfo=UTC)  # the 60 minute reminder of a 19:00 event
AFTER_START = datetime(2026, 10, 2, 19, 30, tzinfo=UTC)


# ── Setup helpers ────────────────────────────────────────────

async def make_group(sf, tenant, *, name="Bear Hunt, daily", exclusive=True, window="day"):
    async with sf() as s:
        group = SignupGroup(kingdom_id=tenant["kingdom_id"], name=name, exclusive_roles=exclusive, attendance_window=window)
        s.add(group)
        await s.commit()
        return group.id


async def set_event(sf, event_id, **values):
    async with sf() as s:
        await s.execute(update(Event).where(Event.id == event_id).values(**values))
        await s.commit()


async def map_role(sf, tenant, *, event_id=None, type_id=None, role_id="role-bear", name="Bear Hunt", created=False):
    async with sf() as s:
        s.add(SignupRole(event_id=event_id, type_id=type_id, server_id=tenant["server_id"], role_id=role_id,
                         role_name=name, created_by_samaya=created))
        await s.commit()


async def event_with_reminder(sf, tenant, *, name="Bear Hunt", anchor=DAY, start="19:00", **kw):
    event_id = await make_event(sf, tenant, name=name, anchor=anchor, start=start, interval=None, reminders=(60,), **kw)
    await sync(sf, event_id, now=NOW)
    return event_id


async def post_reminders(sf, fake, now=AT_REMINDER):
    return await run_delivery_tick(sf, fake, now)


def click(custom_id, *, user=USER, guild=GUILD, roles=(), app="app-1", token="tok-1"):
    return {"type": 3, "guild_id": guild, "channel_id": CHAN, "application_id": app, "token": token,
            "member": {"user": {"id": user}, "roles": list(roles)}, "data": {"component_type": 2, "custom_id": custom_id}}


async def press(sf, fake, interaction, now=NOW, run_followup=True):
    """(response, follow-up ran) for one click."""
    async with sf() as db:
        response, followup = await handle_interaction_ex(db, interaction, now, fake, sf)
    if followup is not None and run_followup:
        await followup()
    return response, followup


def reply_text(response):
    return response["data"]["content"]


async def subscriptions(sf):
    async with sf() as s:
        return list((await s.execute(select(EventSubscription))).scalars())


async def rsvps(sf):
    async with sf() as s:
        return list((await s.execute(select(OccurrenceRsvp))).scalars())


@pytest.fixture
def roles(fake):
    fake.add_role(GUILD, "role-bear", "Bear Hunt")
    fake.add_role(GUILD, "role-bear2", "Bear Hunt #2")
    return fake


# ── Pure ─────────────────────────────────────────────────────

class TestIds:
    def test_round_trips(self):
        assert parse_sub(sub_custom_id(12)) == 12
        assert parse_switch(switch_custom_id(12)) == 12
        assert parse_rsvp(rsvp_custom_id(12, DAY)) == (12, DAY)
        assert parse_rsvp_switch(rsvp_switch_custom_id(12, DAY)) == (12, DAY)

    @pytest.mark.parametrize("value", ["", None, 5, "sub:", "sub:a", "sub:1:2", "sub:-1", "sub:1\n", " sub:1", "sub:１", "sub:1234567890", "sw:1:2"])
    def test_hostile_notify_ids_are_ignored(self, value):
        assert parse_sub(value) is None and parse_switch(value) is None

    @pytest.mark.parametrize("value", [
        "", None, "in:1", "in:1:2026100", "in:1:202610021", "in:1:20261302", "in:1:20260230", "in:1:2026-10-02", "in:1:20261002\n",
        "in:a:20261002", "in:1:２０２６１００２", "in:1:20261002:3", "sub:1:20261002"])
    def test_hostile_rsvp_ids_are_ignored(self, value):
        assert parse_rsvp(value) is None
        assert parse_rsvp_switch(value) is None

    def test_a_valid_date_is_required(self):
        assert parse_rsvp("in:1:20261002") == (1, date(2026, 10, 2))
        assert parse_rsvp("in:1:20260229") is None  # 2026 is not a leap year


class TestHashes:
    def test_notify_hash_is_per_role_and_user(self):
        assert sub_hash(1, "r", USER) == sub_hash(1, "r", USER)
        assert sub_hash(1, "r", USER) not in (sub_hash(2, "r", USER), sub_hash(1, "r2", USER), sub_hash(1, "r", OTHER))

    def test_rsvp_hash_is_shared_across_a_group_and_separate_otherwise(self):
        assert rsvp_hash(5, 1, USER) == rsvp_hash(5, 2, USER)
        assert rsvp_hash(None, 1, USER) != rsvp_hash(None, 2, USER)
        assert rsvp_hash(None, 1, USER) != rsvp_hash(5, 1, USER)

    def test_no_hash_contains_the_discord_id(self):
        for value in (sub_hash(1, "r", USER), rsvp_hash(None, 1, USER), rsvp_hash(5, 1, USER)):
            assert USER not in value and len(value) == 64

    def test_hashes_depend_on_the_secret_key(self, monkeypatch):
        before = rsvp_hash(None, 1, USER)
        monkeypatch.setenv("SECRET_KEY", "another-key")
        assert rsvp_hash(None, 1, USER) != before


def _role(**kw):
    return {"id": "r1", "name": "Bear", "permissions": "0", "managed": False, "position": 1, **kw}


class TestRoleSafety:
    def test_a_plain_role_is_fine(self):
        assert unsafe_role_reason(_role(), 10, GUILD) is None

    @pytest.mark.parametrize("label,bit", sorted(DENIED_PERMISSIONS.items()))
    def test_every_denied_permission_is_refused(self, label, bit):
        reason = unsafe_role_reason(_role(permissions=str(bit | 1)), 10, GUILD)
        assert reason and label in reason

    def test_harmless_permissions_are_fine(self):
        assert unsafe_role_reason(_role(permissions=str((1 << 10) | (1 << 11))), 10, GUILD) is None  # view channel, send messages

    def test_everyone_managed_missing_and_unreadable_are_refused(self):
        assert "everyone" in unsafe_role_reason(_role(id=GUILD), 10, GUILD)
        assert "managed" in unsafe_role_reason(_role(managed=True), 10, GUILD)
        assert "does not exist" in unsafe_role_reason(None, 10, GUILD)
        assert "could not be read" in unsafe_role_reason(_role(permissions="not a number"), 10, GUILD)

    def test_a_role_at_or_above_the_bot_is_refused(self):
        assert unsafe_role_reason(_role(position=10), 10, GUILD)
        assert unsafe_role_reason(_role(position=11), 10, GUILD)
        assert unsafe_role_reason(_role(position=9), 10, GUILD) is None

    def test_an_unknown_bot_position_defers_to_discord(self):
        assert unsafe_role_reason(_role(position=999), None, GUILD) is None


class TestWindows:
    def test_weeks_start_on_monday(self):
        assert week_start(date(2026, 10, 5)) == date(2026, 10, 5)   # Monday
        assert week_start(date(2026, 10, 11)) == date(2026, 10, 5)  # Sunday
        assert week_start(date(2026, 10, 12)) == date(2026, 10, 12)

    def test_ranges(self):
        assert window_range(DAY, "day") == (DAY, DAY)
        assert window_range(date(2026, 10, 7), "week") == (date(2026, 10, 5), date(2026, 10, 11))
        assert window_range(DAY, "none") is None

    def test_a_week_across_the_year_end(self):
        assert window_range(date(2026, 12, 31), "week") == (date(2026, 12, 28), date(2027, 1, 3))


class TestText:
    def test_the_count_line_shows_only_above_zero(self):
        assert count_line(0) == "" and count_line(-1) == ""
        assert "12" in count_line(12)

    def test_with_count_fits_the_limit(self):
        text = with_count("x" * 3000, 7, 2000)
        assert len(text) <= 2000 and text.endswith(count_line(7))
        assert with_count("hello", 0) == "hello"

    def test_the_button_row(self, ):
        event = Event(id=9, rsvp_enabled=True, signup_enabled=True, leadership_only=False)
        row = button_row(event, DAY, True)
        assert [b["label"] for b in row[0]["components"]] == ["I'm in", "Notify me"]
        assert [b["custom_id"] for b in row[0]["components"]] == ["in:9:20261002", "sub:9"]
        assert [b["label"] for b in button_row(event, DAY, False)[0]["components"]] == ["I'm in"]
        assert button_row(Event(id=9, rsvp_enabled=False, signup_enabled=True, leadership_only=False), DAY, True)[0]["components"][0]["label"] == "Notify me"
        assert button_row(Event(id=9, rsvp_enabled=False, signup_enabled=False, leadership_only=False), DAY, True) == []
        assert button_row(Event(id=9, rsvp_enabled=True, signup_enabled=True, leadership_only=True), DAY, True) == []


# ── Notify me ────────────────────────────────────────────────

class TestNotifyMe:
    async def test_a_tap_adds_the_role_and_records_it(self, sf, fake, configured, roles):
        event_id = await event_with_reminder(sf, configured)
        await map_role(sf, configured, event_id=event_id)
        response, followup = await press(sf, fake, click(sub_custom_id(event_id)))
        assert response == {"type": 5, "data": {"flags": 64}}  # a deferred private reply, answered before any role call
        assert ("add_role", GUILD, USER, "role-bear") in fake.calls
        assert fake.interaction_edits == [("app-1", "tok-1", "You will now be notified about Bear Hunt.")]
        rows = await subscriptions(sf)
        assert len(rows) == 1 and rows[0].role_id == "role-bear" and USER not in rows[0].voter_hash

    async def test_the_response_is_sent_before_any_role_call(self, sf, fake, configured, roles):
        event_id = await event_with_reminder(sf, configured)
        await map_role(sf, configured, event_id=event_id)
        response, followup = await press(sf, fake, click(sub_custom_id(event_id)), run_followup=False)
        assert response["type"] == 5 and followup is not None
        assert not [c for c in fake.calls if c[0] in ("add_role", "remove_role", "role_context")]

    async def test_a_second_tap_removes_it(self, sf, fake, configured, roles):
        event_id = await event_with_reminder(sf, configured)
        await map_role(sf, configured, event_id=event_id)
        await press(sf, fake, click(sub_custom_id(event_id)))
        await press(sf, fake, click(sub_custom_id(event_id), roles=["role-bear"]))
        assert ("remove_role", GUILD, USER, "role-bear") in fake.calls
        assert fake.interaction_edits[-1][2] == "You will no longer be notified about Bear Hunt."
        assert await subscriptions(sf) == []

    async def test_direction_comes_from_the_members_roles_not_from_our_record(self, sf, fake, configured, roles):
        event_id = await event_with_reminder(sf, configured)
        await map_role(sf, configured, event_id=event_id)
        await press(sf, fake, click(sub_custom_id(event_id)))  # recorded
        await press(sf, fake, click(sub_custom_id(event_id), roles=[]))  # lost the role by other means: a tap adds it again
        assert fake.calls.count(("add_role", GUILD, USER, "role-bear")) == 2
        assert len(await subscriptions(sf)) == 1

    async def test_each_player_is_counted_once(self, sf, fake, configured, roles):
        event_id = await event_with_reminder(sf, configured)
        await map_role(sf, configured, event_id=event_id)
        await press(sf, fake, click(sub_custom_id(event_id), user=USER))
        await press(sf, fake, click(sub_custom_id(event_id), user=OTHER))
        assert len(await subscriptions(sf)) == 2

    async def test_the_type_role_is_the_fallback(self, sf, fake, configured, roles):
        event_id = await event_with_reminder(sf, configured)
        async with sf() as s:
            type_id = (await s.get(Event, event_id)).type_id
        await map_role(sf, configured, type_id=type_id)
        await press(sf, fake, click(sub_custom_id(event_id)))
        assert ("add_role", GUILD, USER, "role-bear") in fake.calls

    async def test_the_events_own_role_beats_the_types(self, sf, fake, configured, roles):
        event_id = await event_with_reminder(sf, configured)
        async with sf() as s:
            type_id = (await s.get(Event, event_id)).type_id
        await map_role(sf, configured, type_id=type_id, role_id="role-bear", name="Bear Hunt")
        await map_role(sf, configured, event_id=event_id, role_id="role-bear2", name="Bear Hunt #2")
        await press(sf, fake, click(sub_custom_id(event_id)))
        assert ("add_role", GUILD, USER, "role-bear2") in fake.calls
        assert ("add_role", GUILD, USER, "role-bear") not in fake.calls

    @pytest.mark.parametrize("what", ["garbage", "unknown", "disabled", "inactive", "leadership", "wrong_guild", "no_role"])
    async def test_anything_not_active_answers_without_touching_discord(self, sf, fake, configured, roles, what):
        event_id = await event_with_reminder(sf, configured)
        if what != "no_role":
            await map_role(sf, configured, event_id=event_id)
        if what == "disabled":
            await set_event(sf, event_id, signup_enabled=False)
        if what == "inactive":
            await set_event(sf, event_id, active=False)
        if what == "leadership":
            await set_event(sf, event_id, leadership_only=True)
        custom = {"garbage": "sub:x", "unknown": sub_custom_id(99999)}.get(what, sub_custom_id(event_id))
        guild = "some-other-guild" if what == "wrong_guild" else GUILD
        response, followup = await press(sf, fake, click(custom, guild=guild))
        assert reply_text(response) == MSG_INACTIVE and response["data"]["flags"] == 64 and followup is None
        assert not [c for c in fake.calls if c[0] in ("add_role", "remove_role", "role_context")]
        assert await subscriptions(sf) == []

    async def test_a_click_outside_a_server_is_inactive(self, sf, fake, configured, roles):
        event_id = await event_with_reminder(sf, configured)
        await map_role(sf, configured, event_id=event_id)
        interaction = click(sub_custom_id(event_id))
        del interaction["guild_id"]
        response, followup = await press(sf, fake, interaction)
        assert reply_text(response) == MSG_INACTIVE and followup is None

    async def test_an_unsafe_role_is_refused_at_click_time_even_if_it_was_fine_when_saved(self, sf, fake, configured, roles):
        event_id = await event_with_reminder(sf, configured)
        await map_role(sf, configured, event_id=event_id)
        fake.add_role(GUILD, "role-bear", "Bear Hunt", permissions=1 << 13)  # someone later gave it Manage Messages
        await press(sf, fake, click(sub_custom_id(event_id)))
        assert not [c for c in fake.calls if c[0] == "add_role"]
        assert fake.interaction_edits[-1][2] == signup.MSG_UNSAFE
        assert await subscriptions(sf) == []
        async with sf() as s:
            assert "Manage Messages" in (await s.execute(select(SignupRole.last_error))).scalar_one()

    async def test_a_deleted_role_is_refused(self, sf, fake, configured):
        event_id = await event_with_reminder(sf, configured)
        await map_role(sf, configured, event_id=event_id)  # the role is not in the guild
        await press(sf, fake, click(sub_custom_id(event_id)))
        assert fake.interaction_edits[-1][2] == signup.MSG_UNSAFE and await subscriptions(sf) == []

    async def test_a_discord_failure_records_nothing_and_stores_the_error(self, sf, fake, configured, roles):
        event_id = await event_with_reminder(sf, configured)
        await map_role(sf, configured, event_id=event_id)
        fake.role_error = "403 The bot cannot manage this role: it needs the Manage Roles permission and a role above this one"
        await press(sf, fake, click(sub_custom_id(event_id)))
        assert fake.interaction_edits[-1][2] == signup.MSG_CANNOT and await subscriptions(sf) == []
        async with sf() as s:
            row = (await s.execute(select(SignupRole))).scalar_one()
            assert "Manage Roles" in row.last_error and row.last_error_at is not None
        fake.role_error = ""
        await press(sf, fake, click(sub_custom_id(event_id)))  # the next success clears it
        async with sf() as s:
            assert (await s.execute(select(SignupRole.last_error))).scalar_one() is None
        assert len(await subscriptions(sf)) == 1

    async def test_other_failures_get_a_generic_message(self, sf, fake, configured, roles):
        event_id = await event_with_reminder(sf, configured)
        await map_role(sf, configured, event_id=event_id)
        fake.role_error = "404 Unknown member"
        await press(sf, fake, click(sub_custom_id(event_id)))
        assert fake.interaction_edits[-1][2] == signup.MSG_FAILED

    async def test_a_role_list_failure_is_reported_not_raised(self, sf, fake, configured, roles):
        event_id = await event_with_reminder(sf, configured)
        await map_role(sf, configured, event_id=event_id)
        fake.role_context_error = "403 Forbidden — bot missing access to roles"
        await press(sf, fake, click(sub_custom_id(event_id)))
        assert fake.interaction_edits[-1][2] == signup.MSG_CANNOT

    async def test_removing_does_not_need_the_role_to_be_safe(self, sf, fake, configured, roles):
        event_id = await event_with_reminder(sf, configured)
        await map_role(sf, configured, event_id=event_id)
        await press(sf, fake, click(sub_custom_id(event_id)))
        fake.add_role(GUILD, "role-bear", "Bear Hunt", permissions=1 << 3)
        await press(sf, fake, click(sub_custom_id(event_id), roles=["role-bear"]))
        assert ("remove_role", GUILD, USER, "role-bear") in fake.calls and await subscriptions(sf) == []

    async def test_a_failed_reply_edit_never_raises(self, sf, fake, configured, roles):
        event_id = await event_with_reminder(sf, configured)
        await map_role(sf, configured, event_id=event_id)
        fake.interaction_edit_error = "404 Unknown Webhook"
        await press(sf, fake, click(sub_custom_id(event_id)))
        assert len(await subscriptions(sf)) == 1  # the role change stands

    async def test_an_unexpected_error_still_edits_the_reply(self, sf, fake, configured, roles, monkeypatch):
        event_id = await event_with_reminder(sf, configured)
        await map_role(sf, configured, event_id=event_id)

        async def boom(*a, **k):
            raise RuntimeError("boom")
        monkeypatch.setattr(fake, "add_member_role", boom)
        await press(sf, fake, click(sub_custom_id(event_id)))
        assert fake.interaction_edits[-1][2] == signup.MSG_FAILED

    async def test_taps_are_rate_limited_per_user(self, sf, fake, configured, roles, monkeypatch):
        event_id = await event_with_reminder(sf, configured)
        await map_role(sf, configured, event_id=event_id)
        monkeypatch.setattr(rate_limit, "DISABLED", False)
        monkeypatch.setattr(signup, "_click_limit", rate_limit.RateLimiter("t", max_requests=2, window_seconds=60))
        results = [reply_text((await press(sf, fake, click(sub_custom_id(event_id))))[0]) if False else (await press(sf, fake, click(sub_custom_id(event_id))))[0] for _ in range(3)]
        assert results[0]["type"] == 5 and results[1]["type"] == 5
        assert reply_text(results[2]) == signup.MSG_SLOW
        other, _ = await press(sf, fake, click(sub_custom_id(event_id), user=OTHER))
        assert other["type"] == 5  # another player has their own budget


class TestAttendanceGroupsForRoles:
    async def _pair(self, sf, tenant, *, exclusive=True):
        group_id = await make_group(sf, tenant, exclusive=exclusive)
        first = await event_with_reminder(sf, tenant, name="Bear Hunt #1", start="19:00")
        second = await event_with_reminder(sf, tenant, name="Bear Hunt #2", start="21:00")
        for event_id in (first, second):
            await set_event(sf, event_id, signup_group_id=group_id)
        await map_role(sf, tenant, event_id=first, role_id="role-bear", name="Bear Hunt #1")
        await map_role(sf, tenant, event_id=second, role_id="role-bear2", name="Bear Hunt #2")
        return first, second

    async def test_a_second_role_in_the_group_offers_a_switch_and_changes_nothing(self, sf, fake, configured, roles):
        first, second = await self._pair(sf, configured)
        response, followup = await press(sf, fake, click(sub_custom_id(second), roles=["role-bear"]))
        assert followup is None and response["data"]["flags"] == 64
        assert "already in Bear Hunt #1" in reply_text(response) and "Switch to Bear Hunt #2?" in reply_text(response)
        button = response["data"]["components"][0]["components"][0]
        assert button["label"] == "Switch" and button["custom_id"] == switch_custom_id(second)
        assert not [c for c in fake.calls if c[0] in ("add_role", "remove_role")]

    async def test_switching_removes_the_old_role_adds_the_new_one_and_moves_the_record(self, sf, fake, configured, roles):
        first, second = await self._pair(sf, configured)
        await press(sf, fake, click(sub_custom_id(first)))
        response, _ = await press(sf, fake, click(switch_custom_id(second), roles=["role-bear"]))
        assert response["type"] == 5
        calls = [c for c in fake.calls if c[0] in ("add_role", "remove_role")]
        assert calls[-2:] == [("remove_role", GUILD, USER, "role-bear"), ("add_role", GUILD, USER, "role-bear2")]
        assert [r.role_id for r in await subscriptions(sf)] == ["role-bear2"]
        assert fake.interaction_edits[-1][2] == "You will now be notified about Bear Hunt #2."

    async def test_a_failed_removal_changes_nothing(self, sf, fake, configured, roles, monkeypatch):
        first, second = await self._pair(sf, configured)
        await press(sf, fake, click(sub_custom_id(first)))

        async def refuse(token, guild, user, role, reason=""):
            return False, "403 Missing permissions"
        monkeypatch.setattr(fake, "remove_member_role", refuse)
        await press(sf, fake, click(switch_custom_id(second), roles=["role-bear"]))
        assert [r.role_id for r in await subscriptions(sf)] == ["role-bear"]
        assert ("add_role", GUILD, USER, "role-bear2") not in fake.calls

    async def test_a_failed_add_puts_the_old_role_back(self, sf, fake, configured, roles, monkeypatch):
        first, second = await self._pair(sf, configured)
        await press(sf, fake, click(sub_custom_id(first)))
        real = fake.add_member_role

        async def only_old(token, guild, user, role, reason=""):
            if role == "role-bear2":
                return False, "403 Missing permissions"
            return await real(token, guild, user, role, reason)
        monkeypatch.setattr(fake, "add_member_role", only_old)
        await press(sf, fake, click(switch_custom_id(second), roles=["role-bear"]))
        assert "role-bear" in fake.member_roles[(GUILD, USER)]
        assert [r.role_id for r in await subscriptions(sf)] == ["role-bear"]

    async def test_a_group_that_allows_several_roles_does_not_ask(self, sf, fake, configured, roles):
        first, second = await self._pair(sf, configured, exclusive=False)
        response, followup = await press(sf, fake, click(sub_custom_id(second), roles=["role-bear"]))
        assert response["type"] == 5 and followup is not None

    async def test_holding_the_roles_of_events_outside_the_group_is_fine(self, sf, fake, configured, roles):
        first, second = await self._pair(sf, configured)
        other = await event_with_reminder(sf, configured, name="Rally")
        await map_role(sf, configured, event_id=other, role_id="role-rally", name="Rally")
        fake.add_role(GUILD, "role-rally", "Rally")
        response, _ = await press(sf, fake, click(sub_custom_id(first), roles=["role-rally"]))
        assert response["type"] == 5

    async def test_a_forged_switch_for_an_event_with_no_role_is_inactive(self, sf, fake, configured, roles):
        event_id = await event_with_reminder(sf, configured)
        response, followup = await press(sf, fake, click(switch_custom_id(event_id)))
        assert reply_text(response) == MSG_INACTIVE and followup is None


# ── I'm in ───────────────────────────────────────────────────

async def posted_event(sf, fake, tenant, **kw):
    event_id = await event_with_reminder(sf, tenant, **kw)
    await post_reminders(sf, fake)
    return event_id


class TestImIn:
    async def test_a_tap_records_attendance_and_answers_at_once(self, sf, fake, configured):
        event_id = await posted_event(sf, fake, configured)
        response, followup = await press(sf, fake, click(rsvp_custom_id(event_id, DAY)))
        assert followup is None and response["type"] == 4 and response["data"]["flags"] == 64
        assert reply_text(response) == "You are in for Bear Hunt on Fri 02 Oct 19:00 UTC. 1 going."
        rows = await rsvps(sf)
        assert len(rows) == 1 and USER not in rows[0].voter_hash and rows[0].occurrence_date == DAY

    async def test_the_count_includes_other_players(self, sf, fake, configured):
        event_id = await posted_event(sf, fake, configured)
        await press(sf, fake, click(rsvp_custom_id(event_id, DAY), user=OTHER))
        response, _ = await press(sf, fake, click(rsvp_custom_id(event_id, DAY)))
        assert reply_text(response).endswith("2 going.")

    async def test_a_second_tap_withdraws(self, sf, fake, configured):
        event_id = await posted_event(sf, fake, configured)
        await press(sf, fake, click(rsvp_custom_id(event_id, DAY)))
        response, _ = await press(sf, fake, click(rsvp_custom_id(event_id, DAY)))
        assert reply_text(response) == "You are out of Bear Hunt on Fri 02 Oct 19:00 UTC." and await rsvps(sf) == []

    async def test_it_makes_no_discord_call(self, sf, fake, configured):
        event_id = await posted_event(sf, fake, configured)
        before = len(fake.calls)
        await press(sf, fake, click(rsvp_custom_id(event_id, DAY)))
        assert len(fake.calls) == before

    async def test_it_needs_no_role(self, sf, fake, configured):
        event_id = await posted_event(sf, fake, configured)
        response, _ = await press(sf, fake, click(rsvp_custom_id(event_id, DAY)))
        assert "You are in" in reply_text(response)

    @pytest.mark.parametrize("what,expected", [
        ("garbage", MSG_INACTIVE), ("unknown_event", MSG_INACTIVE), ("not_an_occurrence", MSG_INACTIVE),
        ("disabled", MSG_INACTIVE), ("inactive", MSG_INACTIVE), ("leadership", MSG_INACTIVE),
        ("wrong_guild", MSG_INACTIVE), ("cancelled", MSG_CANCELLED), ("started", MSG_STARTED)])
    async def test_taps_that_cannot_count(self, sf, fake, configured, what, expected):
        event_id = await posted_event(sf, fake, configured)
        if what == "disabled":
            await set_event(sf, event_id, rsvp_enabled=False)
        if what == "inactive":
            await set_event(sf, event_id, active=False)
        if what == "leadership":
            await set_event(sf, event_id, leadership_only=True)
        if what == "cancelled":
            async with sf() as s:
                await s.execute(update(EventOccurrence).where(EventOccurrence.event_id == event_id).values(status="cancelled"))
                await s.commit()
        custom = {"garbage": "in:x:y", "unknown_event": rsvp_custom_id(99999, DAY),
                  "not_an_occurrence": rsvp_custom_id(event_id, date(2026, 10, 3))}.get(what, rsvp_custom_id(event_id, DAY))
        guild = "elsewhere" if what == "wrong_guild" else GUILD
        now = AFTER_START if what == "started" else NOW
        response, _ = await press(sf, fake, click(custom, guild=guild), now=now)
        assert reply_text(response) == expected and await rsvps(sf) == []

    async def test_a_moved_occurrence_keeps_its_nominal_date_key(self, sf, fake, configured):
        event_id = await posted_event(sf, fake, configured)
        async with sf() as s:
            await s.execute(update(EventOccurrence).where(EventOccurrence.event_id == event_id).values(
                start_datetime_utc_override=datetime(2026, 10, 3, 5, 0, tzinfo=UTC)))
            await s.commit()
        response, _ = await press(sf, fake, click(rsvp_custom_id(event_id, DAY)))
        assert "You are in" in reply_text(response) and (await rsvps(sf))[0].occurrence_date == DAY

    async def test_ungrouped_events_do_not_link_a_player(self, sf, fake, configured):
        first = await posted_event(sf, fake, configured, name="Bear Hunt #1")
        second = await event_with_reminder(sf, configured, name="Rally", start="20:00")
        await post_reminders(sf, fake, datetime(2026, 10, 2, 18, 59, tzinfo=UTC))
        await press(sf, fake, click(rsvp_custom_id(first, DAY)))
        await press(sf, fake, click(rsvp_custom_id(second, DAY)))
        rows = await rsvps(sf)
        assert len(rows) == 2 and rows[0].voter_hash != rows[1].voter_hash

    async def test_rate_limited(self, sf, fake, configured, monkeypatch):
        event_id = await posted_event(sf, fake, configured)
        monkeypatch.setattr(rate_limit, "DISABLED", False)
        monkeypatch.setattr(signup, "_click_limit", rate_limit.RateLimiter("t", max_requests=1, window_seconds=60))
        await press(sf, fake, click(rsvp_custom_id(event_id, DAY)))
        response, _ = await press(sf, fake, click(rsvp_custom_id(event_id, DAY)))
        assert reply_text(response) == signup.MSG_SLOW


class TestWindowedGroups:
    async def _pair(self, sf, fake, tenant, *, window="day", second_day=DAY, second_start="21:00"):
        group_id = await make_group(sf, tenant, window=window)
        first = await event_with_reminder(sf, tenant, name="Bear Hunt #1", start="19:00")
        second = await event_with_reminder(sf, tenant, name="Bear Hunt #2", anchor=second_day, start=second_start)
        for event_id in (first, second):
            await set_event(sf, event_id, signup_group_id=group_id)
        await post_reminders(sf, fake, datetime(2026, 10, 2, 18, 0, 30, tzinfo=UTC))
        await post_reminders(sf, fake, datetime(second_day.year, second_day.month, second_day.day, 19, 0, 30, tzinfo=UTC)
                             if second_start == "21:00" else AT_REMINDER)
        return first, second

    async def test_a_second_event_the_same_day_offers_a_switch(self, sf, fake, configured):
        first, second = await self._pair(sf, fake, configured)
        await press(sf, fake, click(rsvp_custom_id(first, DAY)))
        response, _ = await press(sf, fake, click(rsvp_custom_id(second, DAY)))
        assert "already in Bear Hunt #1 on Fri 02 Oct" in reply_text(response)
        assert response["data"]["components"][0]["components"][0]["custom_id"] == rsvp_switch_custom_id(second, DAY)
        assert [r.event_id for r in await rsvps(sf)] == [first]  # nothing changed

    async def test_switch_withdraws_the_first_and_records_the_second(self, sf, fake, configured):
        first, second = await self._pair(sf, fake, configured)
        await press(sf, fake, click(rsvp_custom_id(first, DAY)))
        response, _ = await press(sf, fake, click(rsvp_switch_custom_id(second, DAY)))
        assert reply_text(response).startswith("Switched to Bear Hunt #2")
        assert [r.event_id for r in await rsvps(sf)] == [second]

    async def test_a_week_window_blocks_other_days_of_the_same_monday_week(self, sf, fake, configured):
        monday_week_other_day = date(2026, 10, 4)  # Sunday, same week as Friday the 2nd
        first, second = await self._pair(sf, fake, configured, window="week", second_day=monday_week_other_day, second_start="21:00")
        await press(sf, fake, click(rsvp_custom_id(first, DAY)))
        response, _ = await press(sf, fake, click(rsvp_custom_id(second, monday_week_other_day)))
        assert "Switch to Bear Hunt #2?" in reply_text(response)

    async def test_the_next_week_is_allowed(self, sf, fake, configured):
        first, second = await self._pair(sf, fake, configured, window="week", second_day=date(2026, 10, 5), second_start="21:00")  # a Monday
        await press(sf, fake, click(rsvp_custom_id(first, DAY)))
        response, _ = await press(sf, fake, click(rsvp_custom_id(second, date(2026, 10, 5)), now=NOW) if False else click(rsvp_custom_id(second, date(2026, 10, 5))), now=NOW)
        assert "You are in for Bear Hunt #2" in reply_text(response)

    async def test_a_different_day_is_allowed_with_a_day_window(self, sf, fake, configured):
        first, second = await self._pair(sf, fake, configured, window="day", second_day=date(2026, 10, 3), second_start="21:00")
        await press(sf, fake, click(rsvp_custom_id(first, DAY)))
        response, _ = await press(sf, fake, click(rsvp_custom_id(second, date(2026, 10, 3))))
        assert "You are in for Bear Hunt #2" in reply_text(response)

    async def test_no_window_allows_everything(self, sf, fake, configured):
        first, second = await self._pair(sf, fake, configured, window="none")
        await press(sf, fake, click(rsvp_custom_id(first, DAY)))
        response, _ = await press(sf, fake, click(rsvp_custom_id(second, DAY)))
        assert "You are in for Bear Hunt #2" in reply_text(response) and len(await rsvps(sf)) == 2

    async def test_a_cancelled_occurrence_does_not_block(self, sf, fake, configured):
        first, second = await self._pair(sf, fake, configured)
        await press(sf, fake, click(rsvp_custom_id(first, DAY)))
        async with sf() as s:
            await s.execute(update(EventOccurrence).where(EventOccurrence.event_id == first).values(status="cancelled"))
            await s.commit()
        response, _ = await press(sf, fake, click(rsvp_custom_id(second, DAY)))
        assert "You are in for Bear Hunt #2" in reply_text(response)

    async def test_another_player_is_not_blocked(self, sf, fake, configured):
        first, second = await self._pair(sf, fake, configured)
        await press(sf, fake, click(rsvp_custom_id(first, DAY), user=OTHER))
        response, _ = await press(sf, fake, click(rsvp_custom_id(second, DAY)))
        assert "You are in for Bear Hunt #2" in reply_text(response)

    async def test_switch_with_nothing_to_switch_just_records(self, sf, fake, configured):
        first, second = await self._pair(sf, fake, configured)
        response, _ = await press(sf, fake, click(rsvp_switch_custom_id(second, DAY)))
        assert "You are in for Bear Hunt #2" in reply_text(response) and len(await rsvps(sf)) == 1


# ── Reminders ────────────────────────────────────────────────

def message_for(fake, channel=CHAN):
    (key, text), = [(k, t) for k, t in fake.messages.items() if k[0] == channel]
    return key, text


class TestReminderButtons:
    async def test_both_buttons_when_a_role_is_mapped(self, sf, fake, configured, roles):
        event_id = await event_with_reminder(sf, configured)
        await map_role(sf, configured, event_id=event_id)
        await post_reminders(sf, fake)
        key, _ = message_for(fake)
        labels = [b["label"] for b in fake.components[key][0]["components"]]
        assert labels == ["I'm in", "Notify me"]

    async def test_only_im_in_without_a_role(self, sf, fake, configured):
        await event_with_reminder(sf, configured)
        await post_reminders(sf, fake)
        key, _ = message_for(fake)
        assert [b["label"] for b in fake.components[key][0]["components"]] == ["I'm in"]

    async def test_no_buttons_when_both_are_off(self, sf, fake, configured):
        event_id = await event_with_reminder(sf, configured)
        await set_event(sf, event_id, signup_enabled=False, rsvp_enabled=False)
        await post_reminders(sf, fake)
        key, _ = message_for(fake)
        assert fake.components[key] == []

    async def test_a_leadership_only_event_never_gets_buttons(self, sf, fake, tenant):
        await add_audience(sf, tenant, "chan-lead", "", label="Leaders", leadership=True)
        event_id = await event_with_reminder(sf, tenant, leadership_only=True)
        await map_role(sf, tenant, event_id=event_id)
        await post_reminders(sf, fake)
        key, _ = message_for(fake, "chan-lead")
        assert fake.components[key] == []

    async def test_a_shared_channel_carries_one_set_of_buttons(self, sf, fake, tenant, second_tenant):
        await add_audience(sf, tenant, "chan-shared", "", label="A", tenant_id=tenant["id"])
        event_id = await make_event(sf, tenant, interval=None, reminders=(60,), audience=[tenant["id"]])
        await sync(sf, event_id, now=NOW)
        await post_reminders(sf, fake)
        key, _ = message_for(fake, "chan-shared")
        assert len(fake.components[key]) == 1

    async def test_the_subscriber_ping_is_off_by_default(self, sf, fake, configured, roles):
        event_id = await event_with_reminder(sf, configured)
        await map_role(sf, configured, event_id=event_id)
        await post_reminders(sf, fake)
        assert "<@&role-bear>" not in message_for(fake)[1]

    async def test_the_subscriber_ping_is_added_when_asked(self, sf, fake, configured, roles):
        event_id = await event_with_reminder(sf, configured)
        await map_role(sf, configured, event_id=event_id)
        await set_event(sf, event_id, signup_mention=True)
        await post_reminders(sf, fake)
        assert message_for(fake)[1].startswith("<@&role-bear> ")

    async def test_the_ping_comes_with_the_audience_role_mention_not_instead(self, sf, fake, configured, roles):
        event_id = await event_with_reminder(sf, configured, mention_role=True)
        await map_role(sf, configured, event_id=event_id)
        await set_event(sf, event_id, signup_mention=True)
        await post_reminders(sf, fake)
        text = message_for(fake)[1]
        assert "<@&role-mod>" in text and "<@&role-bear>" in text

    async def test_the_ping_needs_the_button_enabled(self, sf, fake, configured, roles):
        event_id = await event_with_reminder(sf, configured)
        await map_role(sf, configured, event_id=event_id)
        await set_event(sf, event_id, signup_mention=True, signup_enabled=False)
        await post_reminders(sf, fake)
        assert "<@&role-bear>" not in message_for(fake)[1]

    async def test_the_audience_role_is_never_duplicated(self, sf, fake, configured, roles):
        event_id = await event_with_reminder(sf, configured, mention_role=True)
        await map_role(sf, configured, event_id=event_id, role_id="role-mod")
        await set_event(sf, event_id, signup_mention=True)
        await post_reminders(sf, fake)
        assert message_for(fake)[1].count("<@&role-mod>") == 1

    async def test_an_earlier_headcount_is_on_a_later_reminder(self, sf, fake, configured):
        event_id = await make_event(sf, configured, interval=None, reminders=(60, 15))
        await sync(sf, event_id, now=NOW)
        await post_reminders(sf, fake)
        await press(sf, fake, click(rsvp_custom_id(event_id, DAY)))
        await post_reminders(sf, fake, datetime(2026, 10, 2, 18, 45, 30, tzinfo=UTC))
        texts = [t for (c, _), t in fake.messages.items()]
        assert any(t.endswith(count_line(1)) for t in texts) and any(not t.endswith(count_line(1)) for t in texts)

    async def test_existing_behaviour_without_buttons_is_unchanged(self, sf, fake, configured):
        event_id = await event_with_reminder(sf, configured, message="Hello {event_time}")
        await set_event(sf, event_id, signup_enabled=False, rsvp_enabled=False)
        await post_reminders(sf, fake)
        assert message_for(fake)[1].startswith("Hello ") and "going" not in message_for(fake)[1]


class TestHeadcountRefresh:
    async def _setup(self, sf, fake, tenant):
        event_id = await posted_event(sf, fake, tenant)
        key, base = message_for(fake)
        return event_id, key, base

    async def test_a_new_rsvp_edits_the_reminder(self, sf, fake, configured):
        event_id, key, base = await self._setup(sf, fake, configured)
        await press(sf, fake, click(rsvp_custom_id(event_id, DAY)))
        counts = await run_rsvp_refresh(sf, fake, AT_REMINDER + timedelta(minutes=5))
        assert counts["edited"] == 1
        assert fake.messages[key] == base + "\n" + count_line(1)
        assert fake.count("edit") == 1

    async def test_the_buttons_are_resent_with_the_edit(self, sf, fake, configured):
        event_id, key, _ = await self._setup(sf, fake, configured)
        await press(sf, fake, click(rsvp_custom_id(event_id, DAY)))
        await run_rsvp_refresh(sf, fake, AT_REMINDER + timedelta(minutes=5))
        assert fake.components[key] and fake.components[key][0]["components"][0]["label"] == "I'm in"

    async def test_nothing_changed_means_no_call(self, sf, fake, configured):
        await self._setup(sf, fake, configured)
        counts = await run_rsvp_refresh(sf, fake, AT_REMINDER + timedelta(minutes=5))
        assert counts["unchanged"] == 1 and fake.count("edit") == 0

    async def test_a_withdrawal_removes_the_line(self, sf, fake, configured):
        event_id, key, base = await self._setup(sf, fake, configured)
        await press(sf, fake, click(rsvp_custom_id(event_id, DAY)))
        await run_rsvp_refresh(sf, fake, AT_REMINDER + timedelta(minutes=5))
        await press(sf, fake, click(rsvp_custom_id(event_id, DAY)))
        await run_rsvp_refresh(sf, fake, AT_REMINDER + timedelta(minutes=10))
        assert fake.messages[key] == base

    async def test_edits_are_throttled_to_one_per_two_minutes(self, sf, fake, configured):
        event_id, key, _ = await self._setup(sf, fake, configured)
        await press(sf, fake, click(rsvp_custom_id(event_id, DAY), user=USER))
        t = AT_REMINDER + timedelta(minutes=5)
        await run_rsvp_refresh(sf, fake, t)
        await press(sf, fake, click(rsvp_custom_id(event_id, DAY), user=OTHER))
        assert (await run_rsvp_refresh(sf, fake, t + timedelta(seconds=60)))["skipped"] == 1
        assert (await run_rsvp_refresh(sf, fake, t + timedelta(minutes=2, seconds=1)))["edited"] == 1
        assert fake.messages[key].endswith(count_line(2))

    async def test_a_failed_edit_backs_off_for_ten_minutes(self, sf, fake, configured):
        event_id, key, _ = await self._setup(sf, fake, configured)
        await press(sf, fake, click(rsvp_custom_id(event_id, DAY)))
        fake.edit_error = "500 Discord server error"
        t = AT_REMINDER + timedelta(minutes=5)
        assert (await run_rsvp_refresh(sf, fake, t))["error"] == 1
        fake.edit_error = ""
        assert (await run_rsvp_refresh(sf, fake, t + timedelta(minutes=5)))["skipped"] == 1
        assert (await run_rsvp_refresh(sf, fake, t + timedelta(minutes=11)))["edited"] == 1

    async def test_a_deleted_message_stops_further_edits(self, sf, fake, configured):
        event_id, key, _ = await self._setup(sf, fake, configured)
        await press(sf, fake, click(rsvp_custom_id(event_id, DAY)))
        del fake.messages[key]
        await run_rsvp_refresh(sf, fake, AT_REMINDER + timedelta(minutes=5))
        calls = fake.count("edit")
        await run_rsvp_refresh(sf, fake, AT_REMINDER + timedelta(minutes=30))
        assert fake.count("edit") == calls
        async with sf() as s:
            assert (await s.execute(select(Delivery.message_deleted_at).where(Delivery.kind == "reminder"))).scalars().first() is not None

    async def test_a_started_occurrence_is_never_edited(self, sf, fake, configured):
        event_id, key, base = await self._setup(sf, fake, configured)
        await press(sf, fake, click(rsvp_custom_id(event_id, DAY)))
        counts = await run_rsvp_refresh(sf, fake, AFTER_START)
        assert fake.count("edit") == 0 and fake.messages[key] == base and not counts["edited"]

    async def test_a_cancelled_occurrence_is_not_edited(self, sf, fake, configured):
        event_id, key, _ = await self._setup(sf, fake, configured)
        await press(sf, fake, click(rsvp_custom_id(event_id, DAY)))
        async with sf() as s:
            await s.execute(update(EventOccurrence).where(EventOccurrence.event_id == event_id).values(status="cancelled"))
            await s.commit()
        await run_rsvp_refresh(sf, fake, AT_REMINDER + timedelta(minutes=5))
        assert fake.count("edit") == 0

    async def test_the_edit_pings_nobody(self, sf, fake, configured, monkeypatch):
        event_id, key, _ = await self._setup(sf, fake, configured)
        await press(sf, fake, click(rsvp_custom_id(event_id, DAY)))
        seen = {}
        real = fake.edit_channel_message

        async def spy(token, channel, message, content, *, no_mentions=False, components=None):
            seen["no_mentions"] = no_mentions
            return await real(token, channel, message, content, no_mentions=no_mentions, components=components)
        monkeypatch.setattr(fake, "edit_channel_message", spy)
        await run_rsvp_refresh(sf, fake, AT_REMINDER + timedelta(minutes=5))
        assert seen["no_mentions"] is True

    async def test_at_most_twenty_edits_per_call(self, sf, fake, configured, monkeypatch):
        event_id, key, _ = await self._setup(sf, fake, configured)
        await press(sf, fake, click(rsvp_custom_id(event_id, DAY)))
        monkeypatch.setattr(signup, "REFRESH_BATCH", 0)
        counts = await run_rsvp_refresh(sf, fake, AT_REMINDER + timedelta(minutes=5))
        assert fake.count("edit") == 0 and counts["skipped"] == 1

    async def test_events_with_im_in_off_are_left_alone(self, sf, fake, configured):
        event_id, key, _ = await self._setup(sf, fake, configured)
        await press(sf, fake, click(rsvp_custom_id(event_id, DAY)))
        await set_event(sf, event_id, rsvp_enabled=False)
        await run_rsvp_refresh(sf, fake, AT_REMINDER + timedelta(minutes=5))
        assert fake.count("edit") == 0

    async def test_a_hostile_event_name_cannot_break_the_text(self, sf, fake, configured):
        event_id = await posted_event(sf, fake, configured, name="@everyone **x**")
        key, base = message_for(fake)
        await press(sf, fake, click(rsvp_custom_id(event_id, DAY)))
        await run_rsvp_refresh(sf, fake, AT_REMINDER + timedelta(minutes=5))
        assert len(fake.messages[key]) <= 2000


# ── Board ────────────────────────────────────────────────────

class TestBoard:
    async def _board(self, sf, tenant, now):
        from models.db import AudienceDestination
        async with sf() as s:
            dest = (await s.execute(select(AudienceDestination))).scalars().first()
            dest.board_scope = "alliance"
            dest.board_tenant_id = tenant["id"]
            await s.commit()
            dest = (await s.execute(select(AudienceDestination))).scalars().first()
            return await board_lines(s, dest, now)

    async def test_the_line_shows_the_count_only_above_zero(self, sf, fake, configured):
        event_id = await posted_event(sf, fake, configured)
        _, lines = await self._board(sf, configured, NOW)
        assert [line.going for line in lines] == [0]
        assert "· 0 in" not in render_board("T", lines, NOW.date()) and " in\n" not in render_board("T", lines, NOW.date())
        await press(sf, fake, click(rsvp_custom_id(event_id, DAY), user=USER))
        await press(sf, fake, click(rsvp_custom_id(event_id, DAY), user=OTHER))
        _, lines = await self._board(sf, configured, NOW)
        assert lines[0].going == 2 and "· 2 in" in render_board("T", lines, NOW.date())

    async def test_a_leadership_only_event_never_reaches_the_board(self, sf, fake, tenant):
        await add_audience(sf, tenant, "chan-lead", "", label="Leaders", leadership=True)
        await add_audience(sf, tenant, "chan-open", "", label="Open")
        event_id = await event_with_reminder(sf, tenant, leadership_only=True)
        async with sf() as s:
            s.add(OccurrenceRsvp(event_id=event_id, occurrence_date=DAY, voter_hash="h" * 64))
            await s.commit()
        _, lines = await self._board(sf, tenant, NOW)
        assert lines == []

    async def test_a_cancelled_line_carries_no_count(self, sf, fake, configured):
        event_id = await posted_event(sf, fake, configured)
        await press(sf, fake, click(rsvp_custom_id(event_id, DAY)))
        async with sf() as s:
            await s.execute(update(EventOccurrence).where(EventOccurrence.event_id == event_id).values(status="cancelled"))
            await s.commit()
        _, lines = await self._board(sf, configured, NOW)
        assert lines[0].cancelled and lines[0].going == 0


# ── Retention and counts ─────────────────────────────────────

class TestRetention:
    async def test_rsvps_go_thirty_days_after_the_occurrence(self, sf, fake, configured):
        event_id = await event_with_reminder(sf, configured)
        async with sf() as s:
            s.add_all([OccurrenceRsvp(event_id=event_id, occurrence_date=date(2026, 9, 1), voter_hash="a" * 64),
                       OccurrenceRsvp(event_id=event_id, occurrence_date=date(2026, 9, 3), voter_hash="b" * 64),
                       OccurrenceRsvp(event_id=event_id, occurrence_date=DAY, voter_hash="c" * 64)])
            await s.commit()
        async with sf() as s:
            assert await prune_rsvps(s, datetime(2026, 10, 2, 0, 5, tzinfo=UTC)) == 1  # Sept 1 is 31 days old, Sept 3 is 29
        assert sorted(r.occurrence_date for r in await rsvps(sf)) == [date(2026, 9, 3), DAY]

    async def test_counts_group_by_occurrence(self, sf, fake, configured):
        event_id = await event_with_reminder(sf, configured)
        async with sf() as s:
            s.add_all([OccurrenceRsvp(event_id=event_id, occurrence_date=DAY, voter_hash=c * 64) for c in "ab"])
            await s.commit()
        async with sf() as s:
            assert await signup.rsvp_counts(s, [(event_id, DAY), (event_id, date(2026, 10, 9))]) == {(event_id, DAY): 2}
            assert await signup.rsvp_counts(s, []) == {}
