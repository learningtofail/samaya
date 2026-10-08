"""Spec §82.10: the two private menus under the schedule board."""
import pytest

from services import rate_limit, signup
from services.board_menus import MSG_EMPTY, menu_custom_id, parse_menu
from services.schedule_board import board_buttons, parse_board_button, refresh_destination
from tests.test_schedule_board import _board, _dest
from tests.test_signup import (
    CHAN, DAY, GUILD, NOW, OTHER, USER, click, event_with_reminder, make_group, map_role, post_reminders, press,
    reply_text, roles, rsvps, set_event, subscriptions,
)

__all__ = ["roles"]  # the fixture is shared with the reminder tests


async def board(sf, fake):
    await _board(sf)
    dest = await _dest(sf)
    await refresh_destination(sf, fake, dest.id, NOW)
    return dest.id


def pick(custom_id, values, roles_held=(), user=USER):
    interaction = click(custom_id, user=user, roles=roles_held)
    interaction["data"].update({"component_type": 3, "values": list(values)})
    return interaction


def options(response):
    return response["data"]["components"][0]["components"][0]["options"]


class TestIds:
    def test_round_trips(self):
        assert parse_board_button("bn:4") == ("bn", 4) and parse_board_button("bi:4") == ("bi", 4)
        assert parse_menu(menu_custom_id("n", 4)) == ("n", 4) and parse_menu(menu_custom_id("i", 4)) == ("i", 4)

    @pytest.mark.parametrize("value", ["", None, 3, "bn:", "bn:a", "bn:1:2", "bn:１", "bx:1", "bn:1\n", "bn:1234567890"])
    def test_hostile_button_ids(self, value):
        assert parse_board_button(value) is None

    @pytest.mark.parametrize("value", ["", None, "bm:x:1", "bm:n:", "bm:n:1:2", "bm:n:１", "bm:n:1\n"])
    def test_hostile_menu_ids(self, value):
        assert parse_menu(value) is None

    def test_the_button_row(self):
        row = board_buttons(7)[0]["components"]
        assert [(b["label"], b["custom_id"]) for b in row] == [("Notify me", "bn:7"), ("I'm in", "bi:7")]


class TestBoardMessage:
    async def test_the_board_carries_both_buttons(self, sf, fake, configured):
        await event_with_reminder(sf, configured)
        dest_id = await board(sf, fake)
        assert fake.components[(CHAN, next(m for (c, m) in fake.messages if c == CHAN))] == board_buttons(dest_id)


class TestNotifyMenu:
    async def test_it_lists_events_with_the_players_roles_ticked(self, sf, fake, configured, roles):
        first = await event_with_reminder(sf, configured, name="Bear Hunt")
        second = await event_with_reminder(sf, configured, name="Arena", start="20:00")
        await map_role(sf, configured, event_id=first)
        await map_role(sf, configured, event_id=second, role_id="role-bear2", name="Arena")
        dest_id = await board(sf, fake)
        response, followup = await press(sf, fake, click(f"bn:{dest_id}", roles=["role-bear2"]))
        assert followup is None and response["type"] == 4 and response["data"]["flags"] == 64
        select = response["data"]["components"][0]["components"][0]
        assert select["type"] == 3 and select["min_values"] == 0 and select["max_values"] == 2
        assert [(o["label"], o["default"]) for o in select["options"]] == [("Arena", True), ("Bear Hunt", False)]

    async def test_events_without_a_role_are_not_offered(self, sf, fake, configured, roles):
        await event_with_reminder(sf, configured)
        dest_id = await board(sf, fake)
        response, _ = await press(sf, fake, click(f"bn:{dest_id}"))
        assert reply_text(response) == MSG_EMPTY

    async def test_submit_adds_and_removes_roles(self, sf, fake, configured, roles):
        first = await event_with_reminder(sf, configured, name="Bear Hunt")
        second = await event_with_reminder(sf, configured, name="Arena", start="20:00")
        await map_role(sf, configured, event_id=first)
        await map_role(sf, configured, event_id=second, role_id="role-bear2", name="Arena")
        dest_id = await board(sf, fake)
        response, _ = await press(sf, fake, pick(f"bm:n:{dest_id}", [str(first)], roles_held=["role-bear2"]))
        assert response["type"] == 7 and response["data"]["components"] == []
        assert ("add_role", GUILD, USER, "role-bear") in fake.calls
        assert ("remove_role", GUILD, USER, "role-bear2") in fake.calls
        text = fake.interaction_edits[-1][2]
        assert "No longer notified: Arena" in text and "Notified: Bear Hunt" in text
        assert [r.role_id for r in await subscriptions(sf)] == ["role-bear"]

    async def test_no_change_makes_no_discord_call(self, sf, fake, configured, roles):
        first = await event_with_reminder(sf, configured)
        await map_role(sf, configured, event_id=first)
        dest_id = await board(sf, fake)
        before = len(fake.calls)
        response, followup = await press(sf, fake, pick(f"bm:n:{dest_id}", [str(first)], roles_held=["role-bear"]))
        assert followup is None and "Nothing changed" in reply_text(response) and len(fake.calls) == before

    async def test_ids_not_offered_are_ignored(self, sf, fake, configured, roles):
        first = await event_with_reminder(sf, configured)
        await map_role(sf, configured, event_id=first)
        dest_id = await board(sf, fake)
        response, followup = await press(sf, fake, pick(f"bm:n:{dest_id}", ["999", "x", "1;2"], roles_held=[]))
        assert followup is None and "Nothing changed" in reply_text(response)
        assert not [c for c in fake.calls if c[0] == "add_role"]

    async def test_two_events_of_one_exclusive_group_cannot_both_be_added(self, sf, fake, configured, roles):
        group_id = await make_group(sf, configured)
        first = await event_with_reminder(sf, configured, name="Bear Hunt #1", start="19:00")
        second = await event_with_reminder(sf, configured, name="Bear Hunt #2", start="21:00")
        for event_id in (first, second):
            await set_event(sf, event_id, signup_group_id=group_id)
        await map_role(sf, configured, event_id=first, role_id="role-bear", name="Bear Hunt #1")
        await map_role(sf, configured, event_id=second, role_id="role-bear2", name="Bear Hunt #2")
        dest_id = await board(sf, fake)
        await press(sf, fake, pick(f"bm:n:{dest_id}", [str(first), str(second)]))
        adds = [c for c in fake.calls if c[0] == "add_role"]
        assert len(adds) == 1
        assert "Skipped" in fake.interaction_edits[-1][2]

    async def test_switching_inside_a_group_works_when_the_old_one_is_unticked(self, sf, fake, configured, roles):
        group_id = await make_group(sf, configured)
        first = await event_with_reminder(sf, configured, name="Bear Hunt #1", start="19:00")
        second = await event_with_reminder(sf, configured, name="Bear Hunt #2", start="21:00")
        for event_id in (first, second):
            await set_event(sf, event_id, signup_group_id=group_id)
        await map_role(sf, configured, event_id=first, role_id="role-bear", name="Bear Hunt #1")
        await map_role(sf, configured, event_id=second, role_id="role-bear2", name="Bear Hunt #2")
        dest_id = await board(sf, fake)
        await press(sf, fake, pick(f"bm:n:{dest_id}", [str(second)], roles_held=["role-bear"]))
        assert ("remove_role", GUILD, USER, "role-bear") in fake.calls
        assert ("add_role", GUILD, USER, "role-bear2") in fake.calls

    async def test_a_menu_from_another_server_is_inactive(self, sf, fake, configured, roles):
        first = await event_with_reminder(sf, configured)
        await map_role(sf, configured, event_id=first)
        dest_id = await board(sf, fake)
        response, _ = await press(sf, fake, click(f"bn:{dest_id}", guild="other-guild"))
        assert reply_text(response) == signup.MSG_INACTIVE
        response, _ = await press(sf, fake, click("bn:99999"))
        assert reply_text(response) == signup.MSG_INACTIVE

    async def test_a_board_that_was_turned_off_is_inactive(self, sf, fake, configured, roles):
        first = await event_with_reminder(sf, configured)
        await map_role(sf, configured, event_id=first)
        dest_id = await board(sf, fake)
        await _board(sf, scope=None)
        response, _ = await press(sf, fake, click(f"bn:{dest_id}"))
        assert reply_text(response) == signup.MSG_INACTIVE

    async def test_taps_are_rate_limited(self, sf, fake, configured, roles, monkeypatch):
        monkeypatch.setattr(rate_limit, "DISABLED", False)
        monkeypatch.setattr(signup, "_click_limit", rate_limit.RateLimiter("t", max_requests=2, window_seconds=60))
        first = await event_with_reminder(sf, configured)
        await map_role(sf, configured, event_id=first)
        dest_id = await board(sf, fake)
        texts = [(await press(sf, fake, click(f"bn:{dest_id}")))[0] for _ in range(3)]
        assert reply_text(texts[-1]) == signup.MSG_SLOW
        other, _ = await press(sf, fake, click(f"bn:{dest_id}", user=OTHER))
        assert "components" in other["data"]


class TestImInMenu:
    async def test_it_lists_upcoming_occurrences_with_attendance_ticked(self, sf, fake, configured):
        event_id = await event_with_reminder(sf, configured)
        await post_reminders(sf, fake)
        dest_id = await board(sf, fake)
        response, _ = await press(sf, fake, click(f"bi:{dest_id}"))
        assert [(o["value"], o["default"]) for o in options(response)] == [(f"{event_id}:{DAY:%Y%m%d}", False)]
        assert "Bear Hunt" in options(response)[0]["label"]
        await press(sf, fake, pick(f"bm:i:{dest_id}", [f"{event_id}:{DAY:%Y%m%d}"]))
        response, _ = await press(sf, fake, click(f"bi:{dest_id}"))
        assert options(response)[0]["default"] is True

    async def test_submit_records_and_withdraws(self, sf, fake, configured):
        event_id = await event_with_reminder(sf, configured)
        await post_reminders(sf, fake)
        dest_id = await board(sf, fake)
        value = f"{event_id}:{DAY:%Y%m%d}"
        response, followup = await press(sf, fake, pick(f"bm:i:{dest_id}", [value]))
        assert followup is None and response["type"] == 7 and "In: Bear Hunt" in reply_text(response)
        assert len(await rsvps(sf)) == 1
        response, _ = await press(sf, fake, pick(f"bm:i:{dest_id}", []))
        assert "Out: Bear Hunt" in reply_text(response) and await rsvps(sf) == []

    async def test_it_makes_no_discord_call(self, sf, fake, configured):
        event_id = await event_with_reminder(sf, configured)
        await post_reminders(sf, fake)
        dest_id = await board(sf, fake)
        before = len(fake.calls)
        await press(sf, fake, pick(f"bm:i:{dest_id}", [f"{event_id}:{DAY:%Y%m%d}"]))
        assert len(fake.calls) == before

    async def test_a_window_clash_is_skipped_and_reported(self, sf, fake, configured):
        group_id = await make_group(sf, configured, window="day")
        first = await event_with_reminder(sf, configured, name="Bear Hunt #1", start="19:00")
        second = await event_with_reminder(sf, configured, name="Bear Hunt #2", start="21:00")
        for event_id in (first, second):
            await set_event(sf, event_id, signup_group_id=group_id)
        await post_reminders(sf, fake)
        dest_id = await board(sf, fake)
        values = [f"{first}:{DAY:%Y%m%d}", f"{second}:{DAY:%Y%m%d}"]
        response, _ = await press(sf, fake, pick(f"bm:i:{dest_id}", values))
        assert "In: Bear Hunt #1" in reply_text(response) and "Skipped Bear Hunt #2" in reply_text(response)
        assert len(await rsvps(sf)) == 1

    async def test_forged_values_are_ignored(self, sf, fake, configured):
        await event_with_reminder(sf, configured)
        dest_id = await board(sf, fake)
        response, _ = await press(sf, fake, pick(f"bm:i:{dest_id}", ["junk", "1:2", "999:20261002"]))
        assert "Nothing changed" in reply_text(response) and await rsvps(sf) == []
