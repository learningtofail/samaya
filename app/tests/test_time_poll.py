"""Spec §84: time polls. Pure rendering and ids, the vote toggle, button clicks through
the interaction handler, the per-minute tick, closing and retention."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from models.db import TimePoll, TimePollMessage, TimePollSlot, TimePollVote
from services import rate_limit, time_poll
from services.discord_commands import handle_interaction
from services.time_poll import (
    SlotView, build_components, cancel_poll, close_poll, custom_id, leading_slots, parse_custom_id, post_poll, prune_polls,
    record_vote, refresh_poll_messages, render_poll, run_poll_tick, voter_hash,
)

UTC = timezone.utc
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
GUILD, CHAN = "test-guild-mod", "chan-poll"
USER = "123456789012345678"


def _views(*votes):
    return [SlotView(i + 1, NOW + timedelta(days=1, hours=i), v) for i, v in enumerate(votes)]


async def make_poll(sf, fake, tenant, *, slots=3, hours=48, title="Bear *Hunt* time", channels=(CHAN,)):
    async with sf() as s:
        poll = TimePoll(kingdom_id=tenant["kingdom_id"], tenant_id=tenant["id"], title=title, status="open", closes_at=NOW + timedelta(hours=hours))
        poll.slots = [TimePollSlot(starts_at=NOW + timedelta(days=1, hours=i), position=i) for i in range(slots)]
        s.add(poll)
        await s.commit()
        poll_id = poll.id
    errors = await post_poll(sf, fake, poll_id, [(None, GUILD, c) for c in channels], NOW)
    assert errors == []
    return poll_id


async def slot_ids(sf, poll_id):
    async with sf() as s:
        return list((await s.execute(select(TimePollSlot.id).where(TimePollSlot.poll_id == poll_id).order_by(TimePollSlot.position))).scalars())


def click(poll_id, slot_id, user=USER, guild=GUILD, channel=CHAN, raw=None):
    return {"type": 3, "guild_id": guild, "channel_id": channel, "member": {"user": {"id": user}},
            "data": {"component_type": 2, "custom_id": raw if raw is not None else custom_id(poll_id, slot_id)}}


async def votes(sf):
    async with sf() as s:
        return list((await s.execute(select(TimePollVote))).scalars())


def only_message(fake, channel=CHAN):
    return next(text for (chan, _), text in fake.messages.items() if chan == channel)


class TestIds:
    def test_round_trip(self):
        assert parse_custom_id(custom_id(12, 345)) == (12, 345)

    @pytest.mark.parametrize("value", ["", None, 5, "tp:1", "tp:1:2:3", "tp:a:b", "tp:-1:2", "tp:1:-2", "xx:1:2", "tp:1234567890:1", "tp:1:2 ", " tp:1:2", "tp:1:2\n", "tp:１:２"])
    def test_anything_else_is_ignored(self, value):
        assert parse_custom_id(value) is None


class TestVoterHash:
    def test_stable_per_poll_and_user(self):
        assert voter_hash(1, USER) == voter_hash(1, USER)

    def test_differs_per_poll_and_per_user(self):
        assert voter_hash(1, USER) != voter_hash(2, USER) and voter_hash(1, USER) != voter_hash(1, "999")

    def test_does_not_contain_the_discord_id(self):
        assert USER not in voter_hash(1, USER) and len(voter_hash(1, USER)) == 64

    def test_depends_on_the_secret_key(self, monkeypatch):
        before = voter_hash(1, USER)
        monkeypatch.setenv("SECRET_KEY", "another-key")
        assert voter_hash(1, USER) != before


class TestRender:
    CLOSES = NOW + timedelta(hours=48)

    def test_every_slot_has_a_number_beside_its_bar(self):
        text = render_poll("Bear", _views(3, 0, 1), "open", self.CLOSES)
        assert "**3** votes" in text and "**0** votes" in text and "**1** vote\n" in text + "\n"
        assert text.count("<t:") == 4  # three slots and the close time

    def test_the_title_is_escaped_and_cannot_ping(self):
        text = render_poll("*Hi* @everyone [x](y)", _views(0, 0), "open", self.CLOSES)
        assert "\\*Hi\\*" in text and "@everyone" not in text and "\\[x\\]" in text

    def test_closed_names_the_leader_a_tie_or_no_votes(self):
        assert "Most votes: option 2" in render_poll("T", _views(1, 4), "closed", self.CLOSES)
        assert "A tie between options 1, 2" in render_poll("T", _views(2, 2, 1), "closed", self.CLOSES)
        assert "No votes were cast" in render_poll("T", _views(0, 0), "closed", self.CLOSES)

    def test_a_chosen_winner_is_marked(self):
        text = render_poll("T", _views(1, 4), "closed", self.CLOSES, winner_slot_id=1)
        assert text.count("✅") == 1 and "Most votes" not in text

    def test_cancelled_says_so(self):
        assert "cancelled" in render_poll("T", _views(1, 1), "cancelled", self.CLOSES)

    def test_leading_slots(self):
        assert leading_slots(_views(0, 0)) == [] and [s.id for s in leading_slots(_views(2, 2, 1))] == [1, 2]


class TestComponents:
    def test_six_slots_are_two_rows_of_five_and_one(self):
        rows = build_components(7, _views(0, 0, 0, 0, 0, 0))
        assert [len(r["components"]) for r in rows] == [5, 1] and all(r["type"] == 1 for r in rows)

    def test_buttons_are_plain_secondary_buttons_with_our_ids(self):
        button = build_components(7, _views(0, 0))[0]["components"][0]
        assert (button["type"], button["style"], button["custom_id"]) == (2, 2, "tp:7:1") and len(button["label"]) <= 80

    def test_no_slots_no_rows(self):
        assert build_components(7, []) == []


class TestPosting:
    async def test_posts_one_message_per_channel_with_buttons(self, sf, fake, configured):
        poll_id = await make_poll(sf, fake, configured, channels=("c1", "c2"))
        assert fake.count("send") == 2 and all(len(rows[0]["components"]) == 3 for rows in fake.components.values())
        async with sf() as s:
            assert len((await s.execute(select(TimePollMessage).where(TimePollMessage.poll_id == poll_id))).scalars().all()) == 2

    async def test_a_failed_post_reports_and_stores_nothing(self, sf, fake, configured):
        fake.post_error = "403 Missing permissions"
        async with sf() as s:
            poll = TimePoll(kingdom_id=configured["kingdom_id"], tenant_id=configured["id"], title="T", status="open", closes_at=NOW + timedelta(hours=1))
            poll.slots = [TimePollSlot(starts_at=NOW + timedelta(days=1), position=0), TimePollSlot(starts_at=NOW + timedelta(days=2), position=1)]
            s.add(poll)
            await s.commit()
            poll_id = poll.id
        errors = await post_poll(sf, fake, poll_id, [(None, GUILD, CHAN)], NOW)
        assert len(errors) == 1 and "403" in errors[0]
        async with sf() as s:
            assert (await s.execute(select(TimePollMessage))).scalars().all() == []


class TestVoting:
    async def test_tapping_toggles_and_counts(self, sf, fake, configured):
        poll_id = await make_poll(sf, fake, configured)
        first, *_ = await slot_ids(sf, poll_id)
        async with sf() as s:
            assert await record_vote(s, poll_id, first, USER, NOW) == "added"
        async with sf() as s:
            assert await record_vote(s, poll_id, first, "777", NOW) == "added"
        assert len(await votes(sf)) == 2
        async with sf() as s:
            assert await record_vote(s, poll_id, first, USER, NOW) == "removed"
        assert len(await votes(sf)) == 1

    async def test_a_person_may_pick_several_slots_but_each_only_once(self, sf, fake, configured):
        poll_id = await make_poll(sf, fake, configured)
        a, b, _ = await slot_ids(sf, poll_id)
        for slot in (a, b):
            async with sf() as s:
                await record_vote(s, poll_id, slot, USER, NOW)
        assert len(await votes(sf)) == 2 and len({v.voter_hash for v in await votes(sf)}) == 1

    async def test_no_discord_id_is_stored_anywhere(self, sf, fake, configured):
        poll_id = await make_poll(sf, fake, configured)
        first, *_ = await slot_ids(sf, poll_id)
        async with sf() as s:
            await record_vote(s, poll_id, first, USER, NOW)
        async with sf() as s:
            dump = ""
            for model in (TimePoll, TimePollSlot, TimePollVote, TimePollMessage):
                for row in (await s.execute(select(model))).scalars().all():
                    dump += repr({c.name: getattr(row, c.name) for c in model.__table__.columns})
        assert USER not in dump

    async def test_the_same_person_hashes_differently_in_two_polls(self, sf, fake, configured):
        one, two = await make_poll(sf, fake, configured), await make_poll(sf, fake, configured, channels=("other",))
        for poll_id in (one, two):
            async with sf() as s:
                await record_vote(s, poll_id, (await slot_ids(sf, poll_id))[0], USER, NOW)
        assert len({v.voter_hash for v in await votes(sf)}) == 2

    async def test_closed_cancelled_unknown_and_foreign_slots(self, sf, fake, configured):
        poll_id, other = await make_poll(sf, fake, configured), await make_poll(sf, fake, configured, channels=("other",))
        mine, theirs = (await slot_ids(sf, poll_id))[0], (await slot_ids(sf, other))[0]
        async with sf() as s:
            assert await record_vote(s, poll_id, theirs, USER, NOW) == "unknown"
            assert await record_vote(s, 99999, mine, USER, NOW) == "unknown"
            assert await record_vote(s, poll_id, mine, USER, NOW + timedelta(hours=49)) == "closed"
        await cancel_poll(sf, fake, poll_id, NOW)
        async with sf() as s:
            assert await record_vote(s, poll_id, mine, USER, NOW) == "closed"
        assert await votes(sf) == []

    async def test_concurrent_clicks_converge(self, tmp_path):
        """Ten people tap at once. Uses a file database: the shared in-memory test engine has one connection,
        so sessions would roll each other back, which Postgres never does."""
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

        from models.db import Base
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'polls.sqlite'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as s:
            poll = TimePoll(kingdom_id=1, tenant_id=1, title="T", status="open", closes_at=NOW + timedelta(hours=1))
            poll.slots = [TimePollSlot(starts_at=NOW + timedelta(days=1), position=0), TimePollSlot(starts_at=NOW + timedelta(days=2), position=1)]
            s.add(poll)
            await s.commit()
            poll_id, first = poll.id, poll.slots[0].id

        async def vote(user):
            async with factory() as s:
                return await record_vote(s, poll_id, first, str(user), NOW)

        await asyncio.gather(*(vote(u) for u in range(1000, 1010)), *(vote(1000) for _ in range(2)))
        async with factory() as s:
            stored = {v.voter_hash for v in (await s.execute(select(TimePollVote))).scalars()}
        await engine.dispose()
        others = {voter_hash(poll_id, str(u)) for u in range(1001, 1010)}
        assert others <= stored and len(stored) in (9, 10)  # user 1000 tapped three times, so is in or out; nobody else is lost


class TestButtonClicks:
    async def test_a_click_answers_with_an_updated_message(self, sf, fake, configured):
        poll_id = await make_poll(sf, fake, configured)
        first, *_ = await slot_ids(sf, poll_id)
        async with sf() as s:
            response = await handle_interaction(s, click(poll_id, first), NOW)
        assert response["type"] == 7 and "**1** vote" in response["data"]["content"]
        assert response["data"]["allowed_mentions"] == {"parse": []} and len(response["data"]["components"][0]["components"]) == 3

    async def test_the_clicked_copy_is_not_edited_again_by_the_tick(self, sf, fake, configured):
        poll_id = await make_poll(sf, fake, configured)
        first, *_ = await slot_ids(sf, poll_id)
        async with sf() as s:
            await handle_interaction(s, click(poll_id, first), NOW)
        await run_poll_tick(sf, fake, NOW + timedelta(minutes=5))
        assert fake.count("edit") == 0

    @pytest.mark.parametrize("raw", ["tp:1:2:3", "garbage", "tp:99999:99999", "tp:-1:1"])
    async def test_hostile_or_unknown_ids_get_a_closed_message(self, sf, fake, configured, raw):
        await make_poll(sf, fake, configured)
        async with sf() as s:
            response = await handle_interaction(s, click(0, 0, raw=raw), NOW)
        assert response["type"] == 4 and response["data"]["flags"] == 64 and "closed" in response["data"]["content"]
        assert await votes(sf) == []

    async def test_a_click_from_another_guild_is_refused(self, sf, fake, configured):
        poll_id = await make_poll(sf, fake, configured)
        first, *_ = await slot_ids(sf, poll_id)
        async with sf() as s:
            response = await handle_interaction(s, click(poll_id, first, guild="some-other-guild"), NOW)
        assert response["type"] == 4 and await votes(sf) == []

    async def test_a_click_without_a_guild_or_user_is_refused(self, sf, fake, configured):
        poll_id = await make_poll(sf, fake, configured)
        first, *_ = await slot_ids(sf, poll_id)
        async with sf() as s:
            no_guild = click(poll_id, first)
            no_guild.pop("guild_id")
            assert (await handle_interaction(s, no_guild, NOW))["type"] == 4
            assert (await handle_interaction(s, click(poll_id, first, user=""), NOW))["type"] == 4

    async def test_a_closed_poll_answers_privately(self, sf, fake, configured):
        poll_id = await make_poll(sf, fake, configured)
        first, *_ = await slot_ids(sf, poll_id)
        await close_poll(sf, fake, poll_id, NOW)
        async with sf() as s:
            response = await handle_interaction(s, click(poll_id, first), NOW)
        assert response["data"]["flags"] == 64 and response["data"]["content"] == "This poll is closed."

    async def test_each_user_is_rate_limited(self, sf, fake, configured, monkeypatch):
        monkeypatch.setattr(rate_limit, "DISABLED", False)
        monkeypatch.setattr(time_poll, "_click_limit", rate_limit.RateLimiter("t", max_requests=2, window_seconds=60))
        poll_id = await make_poll(sf, fake, configured)
        first, *_ = await slot_ids(sf, poll_id)
        kinds = []
        for _ in range(3):
            async with sf() as s:
                kinds.append((await handle_interaction(s, click(poll_id, first), NOW))["type"])
        assert kinds == [7, 7, 4]


class TestTick:
    async def _vote(self, sf, poll_id, user=USER):
        first, *_ = await slot_ids(sf, poll_id)
        async with sf() as s:
            await record_vote(s, poll_id, first, user, NOW)

    async def test_edits_only_when_the_tally_changed_and_throttles(self, sf, fake, configured):
        poll_id = await make_poll(sf, fake, configured)
        await run_poll_tick(sf, fake, NOW + timedelta(minutes=5))
        assert fake.count("edit") == 0
        await self._vote(sf, poll_id)
        await run_poll_tick(sf, fake, NOW + timedelta(minutes=5))
        assert fake.count("edit") == 1 and "**1** vote" in only_message(fake)
        await self._vote(sf, poll_id, "2")
        await run_poll_tick(sf, fake, NOW + timedelta(minutes=5, seconds=30))
        assert fake.count("edit") == 1  # inside the 2 minute throttle
        await run_poll_tick(sf, fake, NOW + timedelta(minutes=8))
        assert fake.count("edit") == 2 and "**2** votes" in only_message(fake)
        await run_poll_tick(sf, fake, NOW + timedelta(minutes=12))
        assert fake.count("edit") == 2

    async def test_other_channels_catch_up(self, sf, fake, configured):
        poll_id = await make_poll(sf, fake, configured, channels=("c1", "c2"))
        await self._vote(sf, poll_id)
        await run_poll_tick(sf, fake, NOW + timedelta(minutes=5))
        assert all("**1** vote" in only_message(fake, c) for c in ("c1", "c2"))

    async def test_expiry_closes_and_removes_the_buttons(self, sf, fake, configured):
        poll_id = await make_poll(sf, fake, configured, hours=1)
        await self._vote(sf, poll_id)
        totals = await run_poll_tick(sf, fake, NOW + timedelta(hours=2))
        assert totals["closed"] == 1 and fake.components[next(iter(fake.components))] == []
        assert "closed" in only_message(fake) and "Most votes" in only_message(fake)
        async with sf() as s:
            poll = await s.get(TimePoll, poll_id)
            assert poll.status == "closed" and poll.closed_at is not None and poll.winner_slot_id is None

    async def test_a_tie_closes_without_a_winner(self, sf, fake, configured):
        poll_id = await make_poll(sf, fake, configured, hours=1)
        a, b, _ = await slot_ids(sf, poll_id)
        for slot, user in ((a, "1"), (b, "2")):
            async with sf() as s:
                await record_vote(s, poll_id, slot, user, NOW)
        await run_poll_tick(sf, fake, NOW + timedelta(hours=2))
        assert "A tie between options 1, 2" in only_message(fake)

    async def test_a_failed_edit_keeps_the_votes_and_retries_after_ten_minutes(self, sf, fake, configured):
        poll_id = await make_poll(sf, fake, configured)
        await self._vote(sf, poll_id)
        fake.edit_error = "500 Discord server error"
        await run_poll_tick(sf, fake, NOW + timedelta(minutes=5))
        async with sf() as s:
            assert "500" in (await s.execute(select(TimePollMessage.error))).scalar_one()
        assert len(await votes(sf)) == 1
        fake.edit_error = ""
        await run_poll_tick(sf, fake, NOW + timedelta(minutes=9))
        assert fake.count("edit") == 1  # still backing off
        await run_poll_tick(sf, fake, NOW + timedelta(minutes=16))
        assert fake.count("edit") == 2
        async with sf() as s:
            assert (await s.execute(select(TimePollMessage.error))).scalar_one() is None

    async def test_a_deleted_message_is_recorded_not_raised(self, sf, fake, configured):
        poll_id = await make_poll(sf, fake, configured)
        await self._vote(sf, poll_id)
        fake.messages.clear()
        result = await refresh_poll_messages(sf, fake, poll_id, NOW + timedelta(minutes=5))
        assert result["error"] == 1
        async with sf() as s:
            assert "deleted" in (await s.execute(select(TimePollMessage.error))).scalar_one()

    async def test_one_channel_raising_does_not_stop_the_others(self, sf, fake, configured):
        poll_id = await make_poll(sf, fake, configured, channels=("c1", "c2"))
        await self._vote(sf, poll_id)
        original = fake.edit_channel_message

        async def flaky(token, channel_id, *args, **kwargs):
            if channel_id == "c1":
                raise RuntimeError("boom")
            return await original(token, channel_id, *args, **kwargs)

        fake.edit_channel_message = flaky
        result = await refresh_poll_messages(sf, fake, poll_id, NOW + timedelta(minutes=5))
        assert result["error"] == 1 and result["edited"] == 1


class TestCloseAndCancel:
    async def test_closing_by_hand_marks_the_chosen_winner(self, sf, fake, configured):
        poll_id = await make_poll(sf, fake, configured)
        _, second, _ = await slot_ids(sf, poll_id)
        assert await close_poll(sf, fake, poll_id, NOW, winner_slot_id=second) is True
        assert only_message(fake).count("✅") == 1
        assert await close_poll(sf, fake, poll_id, NOW) is False

    async def test_cancelling_removes_the_buttons(self, sf, fake, configured):
        poll_id = await make_poll(sf, fake, configured)
        assert await cancel_poll(sf, fake, poll_id, NOW) is True
        assert "cancelled" in only_message(fake) and fake.components[next(iter(fake.components))] == []


class TestRetention:
    async def test_polls_older_than_90_days_go_with_their_votes(self, sf, fake, configured):
        old, fresh = await make_poll(sf, fake, configured), await make_poll(sf, fake, configured, channels=("other",))
        for poll_id in (old, fresh):
            async with sf() as s:
                await record_vote(s, poll_id, (await slot_ids(sf, poll_id))[0], USER, NOW)
        async with sf() as s:
            (await s.get(TimePoll, old)).created_at = NOW - timedelta(days=91)
            await s.commit()
        assert await prune_polls(sf, NOW) == 1
        async with sf() as s:
            assert [p.id for p in (await s.execute(select(TimePoll))).scalars()] == [fresh]
            assert len((await s.execute(select(TimePollVote))).scalars().all()) == 1
            assert len((await s.execute(select(TimePollSlot))).scalars().all()) == 3

    async def test_nothing_to_prune(self, sf):
        assert await prune_polls(sf, NOW) == 0


async def test_nothing_in_the_public_surface(client_no_session):
    for path in ("/api/time-polls", "/api/events", "/events.ics"):
        assert "time_poll" not in (await client_no_session.get(path)).text.lower()
