"""
Tests for POST /admin/api/occurrences/{occ_id}/post.

This endpoint calls out to Discord, so create_discord_event is monkeypatched
rather than hitting the real API. Covers the happy path, the already-posted
guard, and the concurrent-post race that PostLog's reservation-before-Discord-
call pattern exists to close (two simultaneous posts of the same occurrence
should not both succeed, and should not leave an orphaned Discord event with
no PostLog row).
"""
import asyncio
from datetime import date, datetime, time, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import DiscordConfig, EventDefinition, Occurrence, PostLog


async def _make_postable_occurrence(db_session: AsyncSession) -> tuple[EventDefinition, Occurrence]:
    """An event + occurrence starting well outside the 15-minute cutoff,
    plus a DiscordConfig row so post_occurrence's config check passes."""
    today = date.today()
    event = EventDefinition(
        name="Siege Prep",
        interval_days=7,
        start_time_utc=time(19, 0),
        duration_hours=1.0,
        discord_channel="#alliance-war",
        description="Weekly siege prep",
        category="Alliance",
        alliance="Server",
        anchor_date=today,
        notification_channel_id="",
        notification_role_id="",
    )
    db_session.add(event)
    await db_session.commit()
    await db_session.refresh(event)

    start_dt = datetime.now(timezone.utc) + timedelta(hours=2)
    occ = Occurrence(
        event_id=event.id,
        occurrence_date=start_dt.date(),
        start_datetime_utc=start_dt,
        end_datetime_utc=start_dt + timedelta(hours=1),
        post_status="pending",
    )
    db_session.add(occ)
    await db_session.commit()
    await db_session.refresh(occ)

    db_session.add(DiscordConfig(
        id=1, bot_token="fake-token", guild_id="123", public_key="ab" * 32,
    ))
    await db_session.commit()

    return event, occ


class TestPostOccurrence:

    async def test_post_creates_postlog_and_marks_posted(
        self, client: AsyncClient, db_session: AsyncSession, monkeypatch
    ):
        _, occ = await _make_postable_occurrence(db_session)

        async def fake_create(**kwargs):
            return "discord-event-1", ""
        monkeypatch.setattr("routers.admin.occurrences.create_discord_event", fake_create)

        r = await client.post(f"/admin/api/occurrences/{occ.id}/post")
        assert r.status_code == 200, r.text
        assert r.json()["discord_event_id"] == "discord-event-1"

        logs = (await db_session.execute(select(PostLog))).scalars().all()
        assert len(logs) == 1
        assert logs[0].status == "posted"
        assert logs[0].discord_event_id == "discord-event-1"

    async def test_post_when_discord_call_fails_marks_error_not_posted(
        self, client: AsyncClient, db_session: AsyncSession, monkeypatch
    ):
        _, occ = await _make_postable_occurrence(db_session)

        async def fake_create(**kwargs):
            return "", "403 Forbidden — bot missing MANAGE_EVENTS permission"
        monkeypatch.setattr("routers.admin.occurrences.create_discord_event", fake_create)

        r = await client.post(f"/admin/api/occurrences/{occ.id}/post")
        assert r.status_code == 502

        logs = (await db_session.execute(select(PostLog))).scalars().all()
        assert len(logs) == 1
        assert logs[0].status == "error"
        assert not logs[0].discord_event_id

    async def test_second_post_after_success_is_rejected(
        self, client: AsyncClient, db_session: AsyncSession, monkeypatch
    ):
        _, occ = await _make_postable_occurrence(db_session)

        async def fake_create(**kwargs):
            return "discord-event-1", ""
        monkeypatch.setattr("routers.admin.occurrences.create_discord_event", fake_create)

        r1 = await client.post(f"/admin/api/occurrences/{occ.id}/post")
        assert r1.status_code == 200

        r2 = await client.post(f"/admin/api/occurrences/{occ.id}/post")
        assert r2.status_code == 409

        logs = (await db_session.execute(select(PostLog))).scalars().all()
        assert len(logs) == 1

    async def test_concurrent_posts_of_same_occurrence_only_one_succeeds(
        self, client: AsyncClient, db_session: AsyncSession, monkeypatch
    ):
        """
        NOTE: this doesn't actually test the race — see below.

        The regression this endpoint's reservation pattern guards against:
        two near-simultaneous POSTs both pass the initial "does a PostLog
        already exist" check, both call Discord, and only the second
        insert's unique-constraint violation (on event_name +
        occurrence_date) catches the duplicate — by which point a second
        Discord event has already been created with no PostLog row
        pointing at it. The fix reserves the PostLog row (status="pending")
        before calling Discord at all, so the race is caught before any
        Discord call for the loser.

        An asyncio.gather() of two real requests was tried here to exercise
        that race directly, but this test's in-memory SQLite database shares
        a single physical connection across "concurrent" sessions (aiosqlite
        + :memory: doesn't give each session its own connection the way
        asyncpg/Postgres does in production), so the two sessions aren't
        actually isolated from each other — one session's rollback can
        undo the other's already-flushed work, producing a StaleDataError
        that has nothing to do with the guard being tested. That's a
        limitation of this test double, not evidence about the fix.

        What's left to verify by code reading rather than test: the
        reservation INSERT happens (and is flushed) before
        create_discord_event is ever awaited, so a genuine concurrent
        duplicate is caught by uq_post_log at the flush and produces a 409
        with zero Discord calls for the loser — see post_occurrence.
        A real integration test against Postgres could exercise this
        properly if that becomes worth the infrastructure.
        """
        pytest.skip(
            "Race condition isn't reliably testable against this in-memory "
            "SQLite double — see docstring. Covered by code reading instead."
        )
