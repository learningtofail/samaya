"""Ticket board (spec §76): ordering, placing, and the move endpoint."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from models.db import AuditLog, Ticket
from services.ticket_board import BOARD_STATUSES, ordered, place
from sqlalchemy import select

ADMIN = "/admin/api"
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _fake(id, position=None, votes=1, age_days=0):
    return SimpleNamespace(id=id, position=position, upvote_count=votes, created_at=T0 - timedelta(days=age_days))


async def _ticket(db_session, title, status="open", position=None, votes=1):
    t = Ticket(kind="feedback", title=title, description="Some text.", status=status,
               position=position, upvote_count=votes)
    db_session.add(t)
    await db_session.commit()
    return t.id


async def _order(client, status):
    rows = (await client.get(f"{ADMIN}/tickets")).json()
    return [r["id"] for r in sorted(
        (r for r in rows if r["status"] == status),
        key=lambda r: (r["position"] is None, r["position"] or 0))]


class TestOrdering:
    def test_placed_tickets_come_first_by_position(self):
        rows = [_fake(1, votes=99), _fake(2, position=1), _fake(3, position=0)]
        assert [t.id for t in ordered(rows)] == [3, 2, 1]

    def test_unplaced_tickets_sort_by_votes_then_newest_then_id(self):
        rows = [_fake(1, votes=1, age_days=5), _fake(2, votes=3), _fake(3, votes=1, age_days=1), _fake(4, votes=1, age_days=1)]
        assert [t.id for t in ordered(rows)] == [2, 4, 3, 1]

    def test_place_inserts_and_renumbers_the_whole_column(self):
        column = [_fake(1, position=0), _fake(2, position=1), _fake(3, position=2)]
        assert place(column, _fake(9), 1) == {1: 0, 9: 1, 2: 2, 3: 3}

    def test_place_clamps_the_index(self):
        column = [_fake(1, position=0), _fake(2, position=1)]
        assert place(column, _fake(9), 500) == {1: 0, 2: 1, 9: 2}
        assert place(column, _fake(9), 0) == {9: 0, 1: 1, 2: 2}

    def test_place_moves_within_the_same_column_without_duplicating(self):
        column = [_fake(1, position=0), _fake(2, position=1), _fake(3, position=2)]
        assert place(column, column[0], 2) == {2: 0, 3: 1, 1: 2}

    def test_board_statuses_are_the_public_ones_without_dismissed(self):
        assert BOARD_STATUSES == ("open", "planned", "in_progress", "done", "declined")


class TestMoveEndpoint:
    async def test_move_to_another_column_sets_status_and_position(self, client, db_session):
        a = await _ticket(db_session, "A")
        r = await client.post(f"{ADMIN}/tickets/{a}/move", json={"status": "planned", "index": 0})
        assert r.status_code == 200
        body = r.json()
        assert body["ticket"]["status"] == "planned" and body["ticket"]["position"] == 0
        assert body["positions"] == {str(a): 0}

    async def test_move_renumbers_the_destination_and_orders_by_index(self, client, db_session):
        first = await _ticket(db_session, "first", status="planned", position=0)
        second = await _ticket(db_session, "second", status="planned", position=1)
        moved = await _ticket(db_session, "moved")
        await client.post(f"{ADMIN}/tickets/{moved}/move", json={"status": "planned", "index": 1})
        assert await _order(client, "planned") == [first, moved, second]

    async def test_reorder_inside_a_column(self, client, db_session):
        ids = [await _ticket(db_session, f"T{n}", position=n) for n in range(3)]
        await client.post(f"{ADMIN}/tickets/{ids[0]}/move", json={"status": "open", "index": 2})
        assert await _order(client, "open") == [ids[1], ids[2], ids[0]]

    async def test_a_large_index_means_the_end(self, client, db_session):
        a = await _ticket(db_session, "A", status="done", position=0)
        b = await _ticket(db_session, "B")
        await client.post(f"{ADMIN}/tickets/{b}/move", json={"status": "done", "index": 10_000})
        assert await _order(client, "done") == [a, b]

    async def test_dismissed_is_not_a_board_column(self, client, db_session):
        a = await _ticket(db_session, "A")
        r = await client.post(f"{ADMIN}/tickets/{a}/move", json={"status": "dismissed", "index": 0})
        assert r.status_code == 422 and "Dismiss" in r.text

    async def test_unknown_status_and_negative_index_are_rejected(self, client, db_session):
        a = await _ticket(db_session, "A")
        assert (await client.post(f"{ADMIN}/tickets/{a}/move", json={"status": "nope", "index": 0})).status_code == 422
        assert (await client.post(f"{ADMIN}/tickets/{a}/move", json={"status": "open", "index": -1})).status_code == 422

    async def test_a_dismissed_ticket_cannot_be_moved(self, client, db_session):
        a = await _ticket(db_session, "A", status="dismissed")
        r = await client.post(f"{ADMIN}/tickets/{a}/move", json={"status": "open", "index": 0})
        assert r.status_code == 409

    async def test_unknown_ticket_404(self, client):
        assert (await client.post(f"{ADMIN}/tickets/9999/move", json={"status": "open", "index": 0})).status_code == 404

    async def test_viewer_cannot_move(self, db_session, tenant, make_user_and_client):
        viewer, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")])
        a = await _ticket(db_session, "A")
        r = await viewer.post(f"{ADMIN}/tickets/{a}/move", json={"status": "planned", "index": 0},
                              headers={"X-Tenant-Slug": tenant["slug"]})
        assert r.status_code == 403

    async def test_move_is_audited_with_before_and_after(self, client, db_session):
        a = await _ticket(db_session, "A")
        await client.post(f"{ADMIN}/tickets/{a}/move", json={"status": "in_progress", "index": 0})
        row = (await db_session.execute(
            select(AuditLog).where(AuditLog.table_name == "tickets", AuditLog.row_id == a))).scalars().one()
        assert row.action == "update"
        assert json.loads(row.before) == {"status": "open", "position": None}
        assert json.loads(row.after) == {"status": "in_progress", "position": 0}


class TestStatusChangeOutsideTheBoard:
    async def test_patch_status_clears_position(self, client, db_session):
        a = await _ticket(db_session, "A", position=0)
        r = await client.patch(f"{ADMIN}/tickets/{a}", json={"status": "planned"})
        assert r.status_code == 200 and r.json()["position"] is None

    async def test_patch_text_keeps_position(self, client, db_session):
        a = await _ticket(db_session, "A", position=3)
        r = await client.patch(f"{ADMIN}/tickets/{a}", json={"title": "Better title"})
        assert r.json()["position"] == 3


class TestPublicSurfaceStaysClean:
    async def test_position_is_never_public(self, client_no_session, client, db_session):
        a = await _ticket(db_session, "Visible", position=2)
        board = (await client_no_session.get("/api/tickets")).json()
        assert "position" not in board["active"][0]
        assert "position" not in str(board)
        assert board["active"][0]["id"] == a  # the ticket is public; only its board order is private

    async def test_moving_to_done_puts_it_in_the_public_archive(self, client, client_no_session, db_session):
        a = await _ticket(db_session, "Shipped")
        await client.post(f"{ADMIN}/tickets/{a}/move", json={"status": "done", "index": 0})
        board = (await client_no_session.get("/api/tickets")).json()
        assert [t["id"] for t in board["archived"]] == [a] and board["active"] == []

    async def test_admin_list_includes_position(self, client, db_session):
        a = await _ticket(db_session, "A", position=4)
        rows = (await client.get(f"{ADMIN}/tickets")).json()
        assert next(r for r in rows if r["id"] == a)["position"] == 4


@pytest.mark.parametrize("status", BOARD_STATUSES)
async def test_every_column_accepts_a_move(client, db_session, status):
    a = await _ticket(db_session, "A")
    r = await client.post(f"{ADMIN}/tickets/{a}/move", json={"status": status, "index": 0})
    assert r.status_code == 200 and r.json()["ticket"]["status"] == status
