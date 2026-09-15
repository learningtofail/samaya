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

from models.db import EventDefinition, Occurrence, PostLog


async def _make_postable_occurrence(db_session: AsyncSession, tenant: dict) -> tuple[EventDefinition, Occurrence]:
    """An event + occurrence starting well outside the 15-minute cutoff,
    owned by the given tenant (a Tenant already carries its own
    guild_id/bot_token, so there's no separate config row to seed here
    the way the old DiscordConfig singleton needed)."""
    today = date.today()
    event = EventDefinition(
        owning_tenant_id=tenant["id"],
        scope="alliance",
        name="Siege Prep",
        interval_days=7,
        start_time_utc=time(19, 0),
        duration_hours=1.0,
        discord_channel="#alliance-war",
        description="Weekly siege prep",
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
        tenant_id=tenant["id"],
        occurrence_date=start_dt.date(),
        start_datetime_utc=start_dt,
        end_datetime_utc=start_dt + timedelta(hours=1),
        post_status="pending",
    )
    db_session.add(occ)
    await db_session.commit()
    await db_session.refresh(occ)

    return event, occ


class TestPostOccurrence:

    async def test_post_creates_postlog_and_marks_posted(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, monkeypatch
    ):
        _, occ = await _make_postable_occurrence(db_session, tenant)

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
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, monkeypatch
    ):
        _, occ = await _make_postable_occurrence(db_session, tenant)

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
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, monkeypatch
    ):
        _, occ = await _make_postable_occurrence(db_session, tenant)

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
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, monkeypatch
    ):
        """
        NOTE: this doesn't actually test the race — see below.

        The regression this endpoint's reservation pattern guards against:
        two near-simultaneous POSTs both pass the initial "does a PostLog
        already exist" check, both call Discord, and only the second
        insert's unique-constraint violation (on tenant_id + event_name +
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


class TestKingdomWidePost:

    async def test_kingdom_wide_post_fans_out_to_every_tenant_in_kingdom(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, second_tenant: dict, monkeypatch
    ):
        today = date.today()
        event = EventDefinition(
            owning_tenant_id=tenant["id"], scope="kingdom-wide",
            name="Kingdom vs Kingdom", interval_days=14, start_time_utc=time(18, 0),
            duration_hours=3.0, anchor_date=today,
            notification_channel_id="", notification_role_id="",
        )
        db_session.add(event)
        await db_session.commit()
        await db_session.refresh(event)

        start_dt = datetime.now(timezone.utc) + timedelta(hours=2)
        occ = Occurrence(
            event_id=event.id, tenant_id=tenant["id"],
            occurrence_date=start_dt.date(), start_datetime_utc=start_dt,
            end_datetime_utc=start_dt + timedelta(hours=3), post_status="pending",
        )
        db_session.add(occ)
        await db_session.commit()
        await db_session.refresh(occ)

        calls = []
        async def fake_create(**kwargs):
            calls.append(kwargs["guild_id"])
            return f"discord-event-{kwargs['guild_id']}", ""
        monkeypatch.setattr("routers.admin.occurrences.create_discord_event", fake_create)

        r = await client.post(f"/admin/api/occurrences/{occ.id}/post")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "posted"
        assert {t["status"] for t in body["targets"]} == {"posted"}
        assert {t["tenant_slug"] for t in body["targets"]} == {"mod", "nsr"}

        # One independent PostLog row per tenant, not one shared row
        logs = (await db_session.execute(select(PostLog))).scalars().all()
        assert len(logs) == 2
        assert {l.tenant_id for l in logs} == {tenant["id"], second_tenant["id"]}
        assert all(l.status == "posted" for l in logs)
        assert sorted(calls) == sorted(["test-guild-mod", "test-guild-nsr"])

    async def test_kingdom_wide_post_one_tenant_failing_does_not_block_the_other(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, second_tenant: dict, monkeypatch
    ):
        today = date.today()
        event = EventDefinition(
            owning_tenant_id=tenant["id"], scope="kingdom-wide",
            name="Kingdom vs Kingdom", interval_days=14, start_time_utc=time(18, 0),
            duration_hours=3.0, anchor_date=today,
            notification_channel_id="", notification_role_id="",
        )
        db_session.add(event)
        await db_session.commit()
        await db_session.refresh(event)

        start_dt = datetime.now(timezone.utc) + timedelta(hours=2)
        occ = Occurrence(
            event_id=event.id, tenant_id=tenant["id"],
            occurrence_date=start_dt.date(), start_datetime_utc=start_dt,
            end_datetime_utc=start_dt + timedelta(hours=3), post_status="pending",
        )
        db_session.add(occ)
        await db_session.commit()
        await db_session.refresh(occ)

        async def flaky_create(**kwargs):
            if kwargs["guild_id"] == "test-guild-nsr":
                return "", "502 Discord API unavailable"
            return "discord-event-ok", ""
        monkeypatch.setattr("routers.admin.occurrences.create_discord_event", flaky_create)

        r = await client.post(f"/admin/api/occurrences/{occ.id}/post")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "posted"  # any_posted=True overall, even though NSR failed
        by_slug = {t["tenant_slug"]: t for t in body["targets"]}
        assert by_slug["mod"]["status"] == "posted"
        assert by_slug["nsr"]["status"] == "error"

        logs = {l.tenant_id: l for l in (await db_session.execute(select(PostLog))).scalars().all()}
        assert logs[tenant["id"]].status == "posted"
        assert logs[second_tenant["id"]].status == "error"

    async def test_nsr_cancelling_its_own_copy_does_not_touch_mods(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, second_tenant: dict, monkeypatch
    ):
        today = date.today()
        event = EventDefinition(
            owning_tenant_id=tenant["id"], scope="kingdom-wide",
            name="Kingdom vs Kingdom", interval_days=14, start_time_utc=time(18, 0),
            duration_hours=3.0, anchor_date=today,
            notification_channel_id="", notification_role_id="",
        )
        db_session.add(event)
        await db_session.commit()
        await db_session.refresh(event)

        start_dt = datetime.now(timezone.utc) + timedelta(hours=2)
        occ = Occurrence(
            event_id=event.id, tenant_id=tenant["id"],
            occurrence_date=start_dt.date(), start_datetime_utc=start_dt,
            end_datetime_utc=start_dt + timedelta(hours=3), post_status="pending",
        )
        db_session.add(occ)
        await db_session.commit()
        await db_session.refresh(occ)

        async def fake_create(**kwargs):
            return f"discord-event-{kwargs['guild_id']}", ""
        async def fake_cancel(*a, **k):
            return True, ""
        monkeypatch.setattr("routers.admin.occurrences.create_discord_event", fake_create)
        monkeypatch.setattr("routers.admin.occurrences.cancel_discord_event", fake_cancel)

        await client.post(f"/admin/api/occurrences/{occ.id}/post")

        client.headers["X-Tenant-Slug"] = "nsr"
        r = await client.delete(f"/admin/api/occurrences/{occ.id}/discord")
        assert r.status_code == 200, r.text

        logs = {l.tenant_id: l for l in (await db_session.execute(select(PostLog))).scalars().all()}
        assert logs[second_tenant["id"]].status == "cancelled"
        assert logs[tenant["id"]].status == "posted"  # MOD's own copy untouched
