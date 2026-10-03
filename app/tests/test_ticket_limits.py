"""Ticket board WIP limits (spec §76.6)."""
import json

from models.db import AuditLog, Ticket
from sqlalchemy import select

ADMIN = "/admin/api"


async def _ticket(db_session, title, status="open"):
    t = Ticket(kind="feedback", title=title, description="Some text.", status=status)
    db_session.add(t)
    await db_session.commit()
    return t.id


async def _limit(client, **limits):
    return await client.put(f"{ADMIN}/ticket-board/limits", json={"limits": limits})


async def _move_audit(db_session, ticket_id):
    return json.loads((await db_session.execute(
        select(AuditLog).where(AuditLog.table_name == "tickets", AuditLog.row_id == ticket_id))).scalars().one().after)


class TestLimitsApi:
    async def test_defaults_to_no_limits(self, client):
        body = (await client.get(f"{ADMIN}/ticket-board/limits")).json()
        assert body == {"open": None, "planned": None, "in_progress": None, "done": None, "declined": None}

    async def test_set_change_and_clear(self, client):
        assert (await _limit(client, in_progress=3)).json()["in_progress"] == 3
        assert (await _limit(client, in_progress=5, planned=2)).json()["in_progress"] == 5
        body = (await _limit(client, in_progress=None)).json()
        assert body["in_progress"] is None and body["planned"] == 2

    async def test_rejects_bad_values_and_unknown_columns(self, client):
        assert (await _limit(client, in_progress=0)).status_code == 422
        assert (await _limit(client, in_progress=1000)).status_code == 422
        assert (await _limit(client, dismissed=2)).status_code == 422
        assert (await _limit(client, nope=2)).status_code == 422

    async def test_only_superadmin_sets_but_admins_read(self, tenant, make_user_and_client):
        member, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "owner")])
        headers = {"X-Tenant-Slug": tenant["slug"]}
        assert (await member.put(f"{ADMIN}/ticket-board/limits", json={"limits": {"open": 2}}, headers=headers)).status_code == 403
        assert (await member.get(f"{ADMIN}/ticket-board/limits", headers=headers)).status_code == 200

    async def test_change_is_audited(self, client, db_session):
        await _limit(client, planned=4)
        row = (await db_session.execute(select(AuditLog).where(AuditLog.table_name == "ticket_column_limits"))).scalars().one()
        assert json.loads(row.before)["planned"] is None and json.loads(row.after)["planned"] == 4


class TestEnforcement:
    async def test_move_into_a_full_column_is_refused(self, client, db_session):
        await _ticket(db_session, "busy", status="in_progress")
        mover = await _ticket(db_session, "mover")
        await _limit(client, in_progress=1)
        r = await client.post(f"{ADMIN}/tickets/{mover}/move", json={"status": "in_progress", "index": 0})
        assert r.status_code == 409 and "limit of 1" in r.json()["detail"]
        rows = (await client.get(f"{ADMIN}/tickets")).json()
        assert next(t for t in rows if t["id"] == mover)["status"] == "open"

    async def test_override_moves_and_is_audited(self, client, db_session):
        await _ticket(db_session, "busy", status="in_progress")
        mover = await _ticket(db_session, "mover")
        await _limit(client, in_progress=1)
        r = await client.post(f"{ADMIN}/tickets/{mover}/move", json={"status": "in_progress", "index": 0, "override": True})
        assert r.status_code == 200 and r.json()["ticket"]["status"] == "in_progress"
        assert (await _move_audit(db_session, mover))["wip_override"] is True

    async def test_room_left_moves_without_an_audit_flag(self, client, db_session):
        mover = await _ticket(db_session, "mover")
        await _limit(client, in_progress=2)
        r = await client.post(f"{ADMIN}/tickets/{mover}/move", json={"status": "in_progress", "index": 0, "override": True})
        assert r.status_code == 200
        assert "wip_override" not in await _move_audit(db_session, mover)

    async def test_reordering_inside_a_full_column_is_allowed(self, client, db_session):
        a = await _ticket(db_session, "a", status="in_progress")
        await _ticket(db_session, "b", status="in_progress")
        await _limit(client, in_progress=2)
        r = await client.post(f"{ADMIN}/tickets/{a}/move", json={"status": "in_progress", "index": 1})
        assert r.status_code == 200

    async def test_columns_without_a_limit_are_unaffected(self, client, db_session):
        mover = await _ticket(db_session, "mover")
        await _limit(client, in_progress=1)
        assert (await client.post(f"{ADMIN}/tickets/{mover}/move", json={"status": "planned", "index": 0})).status_code == 200

    async def test_limits_never_reach_the_public_board(self, client, client_no_session, db_session):
        await _ticket(db_session, "Visible")
        await _limit(client, open=5)
        assert "limit" not in str((await client_no_session.get("/api/tickets")).json())
