"""Revision a1f0c0de0004 (spec §67.8) against a real Postgres: it seeds
destinations from the old notification settings and per-event overrides, backfills
server Kingdoms, and refuses to guess when it cannot. Runs only when
SAMAYA_MIGRATION_TEST_PG is set (CI sets it; locally point it at any Postgres
superuser URL, e.g. postgresql://postgres:postgres@127.0.0.1:5433)."""
import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest

PG = os.environ.get("SAMAYA_MIGRATION_TEST_PG")
pytestmark = pytest.mark.skipif(not PG, reason="SAMAYA_MIGRATION_TEST_PG not set")
APP_DIR = Path(__file__).resolve().parent.parent


def _alembic(db_name: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": f"{PG.replace('postgresql://', 'postgresql+asyncpg://')}/{db_name}"}
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=APP_DIR, env=env, capture_output=True, text=True)


async def _admin(sql: str):
    conn = await asyncpg.connect(f"{PG}/postgres")
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


async def _db(db_name: str, sql: str | None = None, query: str | None = None):
    conn = await asyncpg.connect(f"{PG}/{db_name}")
    try:
        if sql:
            await conn.execute(sql)
        return await conn.fetch(query) if query else None
    finally:
        await conn.close()


@pytest.fixture
def old_db():
    name = f"mig4_{uuid.uuid4().hex[:8]}"
    asyncio.run(_admin(f"CREATE DATABASE {name}"))
    done = _alembic(name, "upgrade", "a1f0c0de0003")
    assert done.returncode == 0, done.stderr
    yield name
    asyncio.run(_admin(f"DROP DATABASE {name} WITH (FORCE)"))


SEED = """
INSERT INTO kingdoms (id,name,slug) VALUES (1,'K138','k138');
INSERT INTO discord_servers (id,name,guild_id) VALUES (1,'MOD srv','g1'),(2,'HTD srv','g2'),(3,'Spare','g3');
INSERT INTO tenants (id,kingdom_id,server_id,name,slug,color,notification_channel_id,notification_role_id) VALUES
 (1,1,1,'MOD','mod','#111','C1','R1'),(2,1,1,'NSR','nsr','#222','C1','R2'),(3,1,2,'HTD','htd','#333','',''),(4,1,2,'VAL','val','#444','C9','');
INSERT INTO event_types (id,kingdom_id,name,color,default_message,default_reminder_minutes,default_mention_role,sort_order) VALUES (1,1,'General','#000','','[]',false,0);
INSERT INTO events (id,owning_tenant_id,type_id,series_id,name,scope,leadership_only,message,location,start_time_utc,recurrence_kind,anchor_date,mention_role,active)
 VALUES (1,1,1,'s1','Clash','alliance',false,'','', '12:00','none','2026-10-10',false,true),
        (2,1,1,'s2','Leaders','alliance',true,'','', '12:00','none','2026-10-10',false,true),
        (3,1,1,'s3','RoleOnly','alliance',false,'','', '12:00','none','2026-10-10',false,true);
INSERT INTO event_alliances (event_id,tenant_id,notification_channel_id,notification_role_id) VALUES
 (1,1,'CLASH',NULL),(1,2,NULL,NULL),(2,1,'LEAD','RL'),(3,1,NULL,'RX'),(3,4,NULL,NULL);
INSERT INTO event_occurrences (id,event_id,occurrence_date,start_datetime_utc,status) VALUES (1,1,'2026-10-10','2026-10-10 12:00+00','scheduled');
INSERT INTO deliveries (id,occurrence_id,tenant_id,kind,reminder_minutes,due_at_utc,status) VALUES
 (1,1,1,'reminder',10,'2026-10-10 11:50+00','pending'),(2,1,2,'reminder',10,'2026-10-10 11:50+00','pending'),
 (3,1,1,'discord_event',-1,'2026-10-03 12:00+00','pending');
"""


def test_seeds_destinations_and_converts_overrides(old_db):
    asyncio.run(_db(old_db, SEED))
    done = _alembic(old_db, "upgrade", "a1f0c0de0004")
    assert done.returncode == 0, done.stderr

    dests = asyncio.run(_db(old_db, query=(
        "SELECT t.slug, d.server_id, d.channel_id, d.role_id, d.label, d.post_by_default, d.leadership_only "
        "FROM destinations d JOIN tenants t ON t.id = d.tenant_id ORDER BY d.id")))
    assert [tuple(r) for r in dests] == [
        ("mod", 1, "C1", "R1", "Notifications", True, False),
        ("nsr", 1, "C1", "R2", "Notifications", True, False),
        ("val", 2, "C9", "", "Notifications", True, False),
        ("mod", 1, "CLASH", "R1", "Event channel", False, False),   # channel override, inherits the role
        ("mod", 1, "LEAD", "RL", "Event channel 2", False, True),    # leadership event override
        ("mod", 1, "C1", "RX", "Event channel 3", False, False),     # role-only override
    ]
    changes = asyncio.run(_db(old_db, query="SELECT event_id, destination_id, included FROM event_destinations ORDER BY 1,2"))
    assert [tuple(r) for r in changes] == [(1, 1, False), (1, 4, True), (2, 1, False), (2, 5, True), (3, 6, True)]

    rows = asyncio.run(_db(old_db, query="SELECT id, destination_id, guild_id, channel_id FROM deliveries ORDER BY id"))
    assert [tuple(r) for r in rows] == [(1, 1, "g1", "C1"), (2, 2, "g1", "C1"), (3, None, "", "")]
    kingdoms = asyncio.run(_db(old_db, query="SELECT DISTINCT kingdom_id FROM discord_servers"))
    assert [r[0] for r in kingdoms] == [1]


def test_check_is_clean_and_downgrade_round_trips(old_db):
    asyncio.run(_db(old_db, SEED))
    assert _alembic(old_db, "upgrade", "head").returncode == 0
    check = _alembic(old_db, "check")
    assert check.returncode == 0, check.stdout + check.stderr
    assert _alembic(old_db, "downgrade", "a1f0c0de0003").returncode == 0
    assert _alembic(old_db, "upgrade", "head").returncode == 0


def test_a_server_with_no_decidable_kingdom_stops_the_migration(old_db):
    asyncio.run(_db(old_db, (
        "INSERT INTO kingdoms (id,name,slug) VALUES (1,'A','a'),(2,'B','b');"
        "INSERT INTO discord_servers (id,name,guild_id) VALUES (1,'Orphan','g-orphan');")))
    done = _alembic(old_db, "upgrade", "a1f0c0de0004")
    assert done.returncode != 0 and "Orphan" in done.stderr
