"""Tests for the daily 16:00 UTC auto-post job (spec §51,
scheduler/auto_post.py) — for every occurrence in the next 7 days that
isn't posted/cancelled yet: post it if Discord has nothing matching,
quietly record it if a matching Discord event already exists, flag it
if a same-named Discord event's time differs, and otherwise leave it
alone (inactive event, starting too soon, already terminal).

Discord calls (create_discord_event, get_guild_events) are monkeypatched,
same convention as test_post_occurrence.py.
"""
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models.db import EventDefinition, Occurrence, PostLog
from scheduler.auto_post import auto_post_upcoming_occurrences
from services.time_utils import ensure_utc


async def _run_job(db_engine):
    TestSessionLocal = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    await auto_post_upcoming_occurrences(session_factory=TestSessionLocal)


async def _make_occurrence(db_session: AsyncSession, tenant: dict, *, name="Siege Prep",
                            hours_from_now=48, active=True, post_status="pending") -> tuple[EventDefinition, Occurrence]:
    today = date.today()
    event = EventDefinition(
        owning_tenant_id=tenant["id"],
        scope="alliance",
        name=name,
        interval_days=7,
        start_time_utc=time(19, 0),
        duration_hours=1.0,
        discord_channel="#alliance-war",
        description="Weekly siege prep",
        anchor_date=today,
        active=active,
        notification_channel_id="",
        notification_role_id="",
    )
    db_session.add(event)
    await db_session.commit()
    await db_session.refresh(event)

    start_dt = datetime.now(timezone.utc) + timedelta(hours=hours_from_now)
    occ = Occurrence(
        event_id=event.id,
        tenant_id=tenant["id"],
        occurrence_date=start_dt.date(),
        start_datetime_utc=start_dt,
        end_datetime_utc=start_dt + timedelta(hours=1),
        post_status=post_status,
    )
    db_session.add(occ)
    await db_session.commit()
    await db_session.refresh(occ)
    return event, occ


class TestAutoPostNothingOnDiscord:

    async def test_posts_occurrence_with_no_guild_conflict(self, db_engine, db_session: AsyncSession, tenant: dict, monkeypatch):
        event, occ = await _make_occurrence(db_session, tenant)

        async def fake_get_guild_events(token, guild_id):
            return []

        async def fake_create(**kwargs):
            return "discord-evt-1", ""

        monkeypatch.setattr("scheduler.auto_post.get_guild_events", fake_get_guild_events)
        monkeypatch.setattr("services.discord_posting.create_discord_event", fake_create)

        await _run_job(db_engine)

        await db_session.refresh(occ)
        assert occ.post_status == "posted"

        log = (await db_session.execute(select(PostLog).where(PostLog.event_id == event.id))).scalar_one()
        assert log.status == "posted"
        assert log.discord_event_id == "discord-evt-1"


class TestAutoPostSyncsExistingDiscordEvent:

    async def test_matching_discord_event_is_synced_not_duplicated(self, db_engine, db_session: AsyncSession, tenant: dict, monkeypatch):
        event, occ = await _make_occurrence(db_session, tenant)
        start_iso = ensure_utc(occ.start_datetime_utc).isoformat()

        async def fake_get_guild_events(token, guild_id):
            return [{"id": "discord-existing-1", "name": event.name, "scheduled_start_time": start_iso, "status": 1}]

        create_called = False

        async def fake_create(**kwargs):
            nonlocal create_called
            create_called = True
            return "should-not-be-used", ""

        monkeypatch.setattr("scheduler.auto_post.get_guild_events", fake_get_guild_events)
        monkeypatch.setattr("services.discord_posting.create_discord_event", fake_create)

        await _run_job(db_engine)

        assert not create_called
        await db_session.refresh(occ)
        assert occ.post_status == "posted"

        log = (await db_session.execute(select(PostLog).where(PostLog.event_id == event.id))).scalar_one()
        assert log.discord_event_id == "discord-existing-1"
        assert log.status == "posted"
        assert "synced" in log.posted_by


class TestAutoPostFlagsConflict:

    async def test_same_name_different_time_is_flagged_not_posted(self, db_engine, db_session: AsyncSession, tenant: dict, monkeypatch):
        event, occ = await _make_occurrence(db_session, tenant)
        # A same-named Discord event, but scheduled hours away from this
        # occurrence's own start — well outside MATCH_TOLERANCE_MINUTES.
        conflicting_start = (ensure_utc(occ.start_datetime_utc) + timedelta(hours=5)).isoformat()

        async def fake_get_guild_events(token, guild_id):
            return [{"id": "discord-other-1", "name": event.name, "scheduled_start_time": conflicting_start, "status": 1}]

        create_called = False

        async def fake_create(**kwargs):
            nonlocal create_called
            create_called = True
            return "should-not-be-used", ""

        monkeypatch.setattr("scheduler.auto_post.get_guild_events", fake_get_guild_events)
        monkeypatch.setattr("services.discord_posting.create_discord_event", fake_create)

        await _run_job(db_engine)

        assert not create_called
        await db_session.refresh(occ)
        assert occ.post_status == "error"
        assert occ.status_detail is not None
        assert "differ" in occ.status_detail.lower() or "doesn" in occ.status_detail.lower()

        logs = (await db_session.execute(select(PostLog).where(PostLog.event_id == event.id))).scalars().all()
        assert logs == []


class TestAutoPostSkipsIneligible:

    async def test_skips_inactive_event(self, db_engine, db_session: AsyncSession, tenant: dict, monkeypatch):
        event, occ = await _make_occurrence(db_session, tenant, active=False)

        async def fake_get_guild_events(token, guild_id):
            raise AssertionError("should never be called for an inactive event")

        monkeypatch.setattr("scheduler.auto_post.get_guild_events", fake_get_guild_events)

        await _run_job(db_engine)

        await db_session.refresh(occ)
        assert occ.post_status == "pending"

    async def test_skips_occurrence_starting_too_soon(self, db_engine, db_session: AsyncSession, tenant: dict, monkeypatch):
        event, occ = await _make_occurrence(db_session, tenant, hours_from_now=0.1)  # ~6 minutes out

        async def fake_get_guild_events(token, guild_id):
            raise AssertionError("should never be called for an imminent occurrence")

        monkeypatch.setattr("scheduler.auto_post.get_guild_events", fake_get_guild_events)

        await _run_job(db_engine)

        await db_session.refresh(occ)
        assert occ.post_status == "pending"

    async def test_skips_occurrence_outside_seven_day_window(self, db_engine, db_session: AsyncSession, tenant: dict, monkeypatch):
        event, occ = await _make_occurrence(db_session, tenant, hours_from_now=24 * 10)

        async def fake_get_guild_events(token, guild_id):
            raise AssertionError("should never be called for an occurrence outside the window")

        monkeypatch.setattr("scheduler.auto_post.get_guild_events", fake_get_guild_events)

        await _run_job(db_engine)

        await db_session.refresh(occ)
        assert occ.post_status == "pending"

    async def test_skips_already_posted_occurrence(self, db_engine, db_session: AsyncSession, tenant: dict, monkeypatch):
        event, occ = await _make_occurrence(db_session, tenant, post_status="posted")

        async def fake_get_guild_events(token, guild_id):
            raise AssertionError("should never be called for an already-posted occurrence")

        monkeypatch.setattr("scheduler.auto_post.get_guild_events", fake_get_guild_events)

        await _run_job(db_engine)  # should be a no-op; no assertion error means it never queried Discord


class TestAutoPostKingdomWide:

    async def test_kingdom_wide_fans_out_to_every_tenant(self, db_engine, db_session: AsyncSession, tenant: dict, second_tenant: dict, monkeypatch):
        today = date.today()
        event = EventDefinition(
            owning_tenant_id=tenant["id"],
            scope="kingdom-wide",
            name="Kingdom Boss",
            interval_days=7,
            start_time_utc=time(20, 0),
            duration_hours=1.0,
            discord_channel="#kingdom-events",
            description="",
            anchor_date=today,
            notification_channel_id="",
            notification_role_id="",
        )
        db_session.add(event)
        await db_session.commit()
        await db_session.refresh(event)

        start_dt = datetime.now(timezone.utc) + timedelta(hours=48)
        occ = Occurrence(
            event_id=event.id, tenant_id=tenant["id"],
            occurrence_date=start_dt.date(),
            start_datetime_utc=start_dt, end_datetime_utc=start_dt + timedelta(hours=1),
            post_status="pending",
        )
        db_session.add(occ)
        await db_session.commit()
        await db_session.refresh(occ)

        async def fake_get_guild_events(token, guild_id):
            return []

        posted_guilds = []

        async def fake_create(**kwargs):
            posted_guilds.append(kwargs["guild_id"])
            return f"discord-evt-{len(posted_guilds)}", ""

        monkeypatch.setattr("scheduler.auto_post.get_guild_events", fake_get_guild_events)
        monkeypatch.setattr("services.discord_posting.create_discord_event", fake_create)

        await _run_job(db_engine)

        await db_session.refresh(occ)
        assert occ.post_status == "posted"

        logs = (await db_session.execute(select(PostLog).where(PostLog.event_id == event.id))).scalars().all()
        tenant_ids = {log.tenant_id for log in logs}
        assert tenant_ids == {tenant["id"], second_tenant["id"]}
