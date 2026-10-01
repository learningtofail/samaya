"""ops cutover step (spec §66.8): the script that deletes the old system's
Discord Scheduled Events before the closure migration drops post_log."""
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import cutover_delete_old_discord_events as cutover


@pytest.fixture
async def old_db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for ddl in (
            "CREATE TABLE discord_servers (id INTEGER PRIMARY KEY, guild_id TEXT, bot_token TEXT)",
            "CREATE TABLE tenants (id INTEGER PRIMARY KEY, slug TEXT, server_id INTEGER)",
            "CREATE TABLE post_log (id INTEGER PRIMARY KEY, tenant_id INTEGER, event_name TEXT,"
            " occurrence_date TEXT, discord_event_id TEXT)",
            "INSERT INTO discord_servers VALUES (1, 'g1', 'tok1'), (2, 'g2', NULL)",
            "INSERT INTO tenants VALUES (1, 'mod', 1), (2, 'nsr', 1), (3, 'other', 2)",
            # mod and nsr share guild g1, so one real event e1 with two log rows
            "INSERT INTO post_log VALUES (1, 1, 'Bear', '2026-10-05', 'e1'), (2, 2, 'Bear', '2026-10-05', 'e1'),"
            " (3, 3, 'Bear', '2026-10-05', 'e2'), (4, 1, 'Old', '2026-09-01', NULL)",
        ):
            await conn.execute(text(ddl))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(cutover, "AsyncSessionLocal", factory)
    monkeypatch.setenv("PLATFORM_BOT_TOKEN", "platform-token")
    yield factory
    await engine.dispose()


async def _remaining(factory):
    async with factory() as s:
        return sorted((await s.execute(text("SELECT id FROM post_log WHERE discord_event_id IS NOT NULL"))).scalars())


async def test_dry_run_deletes_nothing(old_db, monkeypatch):
    calls = []

    async def fake_cancel(*args):
        calls.append(args)
        return True, ""

    monkeypatch.setattr(cutover, "cancel_discord_event", fake_cancel)
    assert await cutover.main(apply=False) == 0
    assert calls == [] and await _remaining(old_db) == [1, 2, 3]


async def test_apply_deletes_each_shared_event_once_and_clears_ids(old_db, monkeypatch):
    calls = []

    async def fake_cancel(token, guild, event_id):
        calls.append((token, guild, event_id))
        return True, ""

    monkeypatch.setattr(cutover, "cancel_discord_event", fake_cancel)
    assert await cutover.main(apply=True) == 0
    assert sorted(calls) == [("platform-token", "g2", "e2"), ("tok1", "g1", "e1")]
    assert await _remaining(old_db) == []


async def test_already_gone_counts_as_success(old_db, monkeypatch):
    async def fake_cancel(*args):
        return False, "404 Event not found — may already be cancelled"

    monkeypatch.setattr(cutover, "cancel_discord_event", fake_cancel)
    assert await cutover.main(apply=True) == 0
    assert await _remaining(old_db) == []


async def test_failure_keeps_the_id_so_the_migration_guard_still_blocks(old_db, monkeypatch):
    async def fake_cancel(token, guild, event_id):
        return (False, "403 Forbidden") if event_id == "e2" else (True, "")

    monkeypatch.setattr(cutover, "cancel_discord_event", fake_cancel)
    assert await cutover.main(apply=True) == 1
    assert await _remaining(old_db) == [3]
