"""Revision a1f0c0de0005 (spec §68.4) against a real Postgres: destinations
become Kingdom-owned Audiences, identical ones merge, groups become Audiences,
and every reference follows.
Runs only when SAMAYA_MIGRATION_TEST_PG is set (see test_migration_0004.py)."""
import asyncio
import os
import uuid

import pytest

from tests.test_migration_0004 import _admin, _alembic, _db

PG = os.environ.get("SAMAYA_MIGRATION_TEST_PG")
pytestmark = pytest.mark.skipif(not PG, reason="SAMAYA_MIGRATION_TEST_PG not set")


@pytest.fixture
def db_0004():
    name = f"mig5_{uuid.uuid4().hex[:8]}"
    asyncio.run(_admin(f"CREATE DATABASE {name}"))
    done = _alembic(name, "upgrade", "a1f0c0de0004")
    assert done.returncode == 0, done.stderr
    yield name
    asyncio.run(_admin(f"DROP DATABASE {name} WITH (FORCE)"))


# MOD and NSR each have their own copy of the same leadership channel and role
# (the production case), plus their own distinct "Notifications" destinations.
SEED = """
INSERT INTO kingdoms (id,name,slug) VALUES (1,'K138','k138');
INSERT INTO discord_servers (id,name,guild_id,kingdom_id) VALUES (1,'G','g1',1);
INSERT INTO tenants (id,kingdom_id,server_id,name,slug,color) VALUES (1,1,1,'MOD','mod','#111'),(2,1,1,'NSR','nsr','#222');
INSERT INTO destinations (id,tenant_id,server_id,channel_id,role_id,label,post_by_default,leadership_only) VALUES
 (1,1,1,'CN1','','Notifications',true,false),
 (2,2,1,'CN2','','Notifications',true,false),
 (3,1,1,'LEAD','RL','MOD Leadership',true,true),
 (4,2,1,'LEAD','RL','NSR Leadership',false,true);
INSERT INTO event_types (id,kingdom_id,name,color,default_message,default_reminder_minutes,default_mention_role,sort_order) VALUES (1,1,'General','#000','','[]',false,0);
INSERT INTO events (id,owning_tenant_id,type_id,series_id,name,scope,leadership_only,message,location,start_time_utc,recurrence_kind,anchor_date,mention_role,active)
 VALUES (1,1,1,'s1','Leaders','alliance',true,'','','12:00','none','2026-10-10',false,true);
INSERT INTO event_destinations (event_id,destination_id,included) VALUES (1,3,false),(1,4,true);
INSERT INTO audience_groups (id,kingdom_id,name) VALUES (1,1,'Everyone');
INSERT INTO audience_group_destinations (group_id,destination_id) VALUES (1,1),(1,2);
INSERT INTO event_groups (event_id,group_id) VALUES (1,1);
INSERT INTO event_occurrences (id,event_id,occurrence_date,start_datetime_utc,status) VALUES (1,1,'2026-10-10','2026-10-10 12:00+00','scheduled');
INSERT INTO deliveries (id,occurrence_id,tenant_id,destination_id,kind,reminder_minutes,due_at_utc,status,guild_id,channel_id) VALUES
 (1,1,1,3,'reminder',10,'2026-10-10 11:50+00','pending','g1','LEAD'),
 (2,1,2,4,'reminder',10,'2026-10-10 11:50+00','pending','g1','LEAD');
SELECT setval(pg_get_serial_sequence('destinations','id'), 100);
"""


def test_destinations_merge_into_audiences_and_references_follow(db_0004):
    asyncio.run(_db(db_0004, SEED))
    done = _alembic(db_0004, "upgrade", "head")
    assert done.returncode == 0, done.stderr

    audiences = asyncio.run(_db(db_0004, query="SELECT id, kingdom_id, label, leadership_only FROM audiences ORDER BY id"))
    assert [tuple(r) for r in audiences] == [
        (1, 1, "Notifications", False),
        (2, 1, "Notifications (NSR)", False),   # same label, different channel: suffixed
        (3, 1, "MOD Leadership", True),         # NSR's copy merged into the lowest id
        (4, 1, "Everyone", False),              # the group, as an Audience
    ]
    dests = asyncio.run(_db(db_0004, query="SELECT audience_id, channel_id, role_id FROM audience_destinations ORDER BY id"))
    assert [tuple(r) for r in dests] == [
        (1, "CN1", ""), (2, "CN2", ""), (3, "LEAD", "RL"), (4, "CN1", ""), (4, "CN2", "")]
    links = asyncio.run(_db(db_0004, query="SELECT tenant_id, audience_id, post_by_default FROM alliance_audiences ORDER BY 2,1"))
    assert [tuple(r) for r in links] == [(1, 1, True), (2, 2, True), (1, 3, True), (2, 3, False), (1, 4, False)]
    # an opt-out and an add on merged copies leave the opt-out; the group became an add
    changes = asyncio.run(_db(db_0004, query="SELECT event_id, audience_id, included FROM event_audiences ORDER BY 2"))
    assert [tuple(r) for r in changes] == [(1, 3, False), (1, 4, True)]
    rows = asyncio.run(_db(db_0004, query="SELECT id, tenant_id, destination_id FROM deliveries ORDER BY id"))
    assert [tuple(r) for r in rows] == [(1, 1, 3), (2, 2, 3)]  # one destination, two alliances: both rows stay
    tables = asyncio.run(_db(db_0004, query=(
        "SELECT table_name FROM information_schema.tables WHERE table_name IN "
        "('destinations','audience_groups','event_groups','event_destinations')")))
    assert tables == []


def test_check_is_clean_and_downgrade_splits_back(db_0004):
    asyncio.run(_db(db_0004, SEED))
    assert _alembic(db_0004, "upgrade", "head").returncode == 0
    check = _alembic(db_0004, "check")
    assert check.returncode == 0, check.stdout + check.stderr
    down = _alembic(db_0004, "downgrade", "a1f0c0de0004")
    assert down.returncode == 0, down.stderr
    rows = asyncio.run(_db(db_0004, query=(
        "SELECT d.tenant_id, d.channel_id, d.label FROM destinations d WHERE d.channel_id = 'LEAD' ORDER BY d.tenant_id")))
    assert [tuple(r) for r in rows] == [(1, "LEAD", "MOD Leadership"), (2, "LEAD", "MOD Leadership")]
    moved = asyncio.run(_db(db_0004, query="SELECT tenant_id, destination_id FROM deliveries ORDER BY id"))
    assert moved[0][1] != moved[1][1]  # each alliance has its own copy again
    assert _alembic(db_0004, "upgrade", "head").returncode == 0
