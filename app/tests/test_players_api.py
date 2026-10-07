"""Spec §80.3: the player registry API. Permissions, bulk add, edit, move, delete, export."""
import csv
import io

from sqlalchemy import select

from models.db import AuditLog, Kingdom, Player, RedemptionResult

A = "/admin/api"


def _h(tenant):
    return {"X-Tenant-Slug": tenant["slug"]}


async def _add(client, text):
    r = await client.post(f"{A}/players/bulk", json={"text": text})
    assert r.status_code == 200, r.text
    return r.json()


class TestBulkAdd:
    async def test_adds_updates_and_reports(self, client, db_session):
        out = await _add(client, "11111111,245,Bear\n22222222\nabc\n11111111,300")
        assert (out["added"], out["updated"], out["duplicates"]) == (2, 0, 1)
        assert out["rejected"][0]["line"] == 3
        players = (await db_session.execute(select(Player).order_by(Player.id))).scalars().all()
        assert [(p.fid, p.kid, p.name) for p in players] == [("11111111", 300, "Bear"), ("22222222", None, None)]

    async def test_adding_the_same_player_again_changes_nothing_unless_details_differ(self, client):
        await _add(client, "11111111,245,Bear")
        again = await _add(client, "11111111,245,Bear")
        assert (again["added"], again["updated"], again["duplicates"]) == (0, 0, 1)
        changed = await _add(client, "11111111,300")
        assert (changed["updated"], changed["duplicates"]) == (1, 0)

    async def test_a_player_in_another_alliance_is_reported_not_moved(self, client, tenant, second_tenant, db_session):
        await _add(client, "11111111")
        client.headers["X-Tenant-Slug"] = second_tenant["slug"]
        out = await _add(client, "11111111\n22222222")
        assert out["in_other_alliance"] == ["11111111"] and out["added"] == 1
        owner = (await db_session.execute(select(Player.tenant_id).where(Player.fid == "11111111"))).scalar_one()
        assert owner == tenant["id"]

    async def test_too_many_lines_is_422(self, client):
        r = await client.post(f"{A}/players/bulk", json={"text": "\n".join(str(10000000 + i) for i in range(501))})
        assert r.status_code == 422

    async def test_the_alliance_cap_is_enforced(self, client, db_session, tenant, monkeypatch):
        from routers.admin import players
        monkeypatch.setattr(players, "ALLIANCE_CAP", 2)
        out = await _add(client, "11111111\n22222222\n33333333")
        assert out["added"] == 2 and "limit" in out["rejected"][-1]["reason"]

    async def test_audit_row_has_counts_and_no_ids(self, client, db_session):
        await _add(client, "11111111,245,Secret Name")
        row = (await db_session.execute(select(AuditLog).where(AuditLog.table_name == "players"))).scalar_one()
        assert "11111111" not in (row.after or "") and "Secret" not in (row.after or "")
        assert '"added": 1' in row.after

    async def test_viewer_cannot_add(self, tenant, make_user_and_client):
        viewer, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")])
        r = await viewer.post(f"{A}/players/bulk", json={"text": "11111111"}, headers=_h(tenant))
        assert r.status_code == 403

    async def test_no_access_to_the_alliance_is_403(self, tenant, second_tenant, make_user_and_client):
        other, _ = await make_user_and_client(tenant_grants=[(second_tenant["id"], "owner")])
        r = await other.post(f"{A}/players/bulk", json={"text": "11111111"}, headers=_h(tenant))
        assert r.status_code == 403


class TestList:
    async def test_lists_with_effective_kingdom_and_missing_count(self, client, tenant, db_session):
        await _add(client, "11111111\n22222222,245")
        out = (await client.get(f"{A}/players")).json()
        assert out["missing_kingdom"] == 1
        assert {p["fid"]: p["effective_kid"] for p in out["players"]} == {"11111111": None, "22222222": 245}
        (await db_session.get(Kingdom, tenant["kingdom_id"])).game_number = 138
        await db_session.commit()
        out = (await client.get(f"{A}/players")).json()
        assert out["missing_kingdom"] == 0 and out["game_numbers"] == {tenant["slug"]: 138}

    async def test_reads_are_scoped_to_the_callers_alliances(self, client, tenant, second_tenant, make_user_and_client):
        await _add(client, "11111111")
        client.headers["X-Tenant-Slug"] = second_tenant["slug"]
        await _add(client, "22222222")
        other, _ = await make_user_and_client(tenant_grants=[(second_tenant["id"], "viewer")])
        everything = (await other.get(f"{A}/players", headers={"X-Tenant-Slug": "*"})).json()
        assert [p["fid"] for p in everything["players"]] == ["22222222"]
        assert (await other.get(f"{A}/players", headers=_h(tenant))).status_code == 403


class TestEditMoveDelete:
    async def _one(self, client):
        await _add(client, "11111111")
        return (await client.get(f"{A}/players")).json()["players"][0]["id"]

    async def test_edit_and_clear_the_kingdom_override(self, client):
        pid = await self._one(client)
        r = await client.patch(f"{A}/players/{pid}", json={"kid": 245, "name": " Bear ", "note": "R4"})
        assert (r.json()["kid"], r.json()["name"], r.json()["note"]) == (245, "Bear", "R4")
        assert (await client.patch(f"{A}/players/{pid}", json={"kid": None})).json()["kid"] is None

    async def test_move_to_another_alliance(self, client, tenant, second_tenant, db_session):
        pid = await self._one(client)
        r = await client.patch(f"{A}/players/{pid}", json={"tenant_slug": second_tenant["slug"]})
        assert r.status_code == 200 and r.json()["tenant_slug"] == second_tenant["slug"]
        assert (await db_session.get(Player, pid)).tenant_id == second_tenant["id"]

    async def test_move_needs_write_access_to_the_destination(self, tenant, second_tenant, make_user_and_client):
        coord, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "coordinator"), (second_tenant["id"], "viewer")])
        await coord.post(f"{A}/players/bulk", json={"text": "11111111"}, headers=_h(tenant))
        pid = (await coord.get(f"{A}/players", headers=_h(tenant))).json()["players"][0]["id"]
        r = await coord.patch(f"{A}/players/{pid}", json={"tenant_slug": second_tenant["slug"]}, headers=_h(tenant))
        assert r.status_code == 403

    async def test_a_player_of_another_alliance_is_404(self, client, second_tenant):
        pid = await self._one(client)
        client.headers["X-Tenant-Slug"] = second_tenant["slug"]
        assert (await client.patch(f"{A}/players/{pid}", json={"name": "x"})).status_code == 404
        assert (await client.delete(f"{A}/players/{pid}")).status_code == 404

    async def test_delete_removes_the_player_and_their_redemption_rows(self, client, db_session):
        pid = await self._one(client)
        from models.db import RedemptionRun
        db_session.add(RedemptionRun(id=1, kingdom_id=1, code="X1Y2Z3", status="done"))
        await db_session.flush()
        tenant_id = (await db_session.get(Player, pid)).tenant_id
        db_session.add(RedemptionResult(run_id=1, tenant_id=tenant_id, player_id=pid, fid="11111111", kid=138, status="SUCCESS", attempts=1, cooldowns=0))
        await db_session.commit()
        assert (await client.delete(f"{A}/players/{pid}")).status_code == 204
        assert (await db_session.execute(select(RedemptionResult))).first() is None
        assert (await db_session.execute(select(Player))).first() is None
        row = (await db_session.execute(select(AuditLog).where(AuditLog.action == "delete"))).scalar_one()
        assert "11111111" not in (row.before or "")

    async def test_viewer_cannot_edit_or_delete(self, client, tenant, make_user_and_client):
        pid = await self._one(client)
        viewer, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")])
        assert (await viewer.patch(f"{A}/players/{pid}", json={"name": "x"}, headers=_h(tenant))).status_code == 403
        assert (await viewer.delete(f"{A}/players/{pid}", headers=_h(tenant))).status_code == 403


class TestExport:
    async def test_csv_with_formula_guard_and_a_count_only_audit(self, client, db_session, tenant):
        await _add(client, "11111111,245,=HYPERLINK(1)\n22222222,245,Plain")
        r = await client.get(f"{A}/players/export")
        assert r.status_code == 200 and "attachment" in r.headers["content-disposition"] and r.headers["cache-control"] == "no-store"
        rows = list(csv.reader(io.StringIO(r.text)))
        assert rows[0] == ["fid", "kid", "name", "alliance"]
        assert rows[1][2] == "'=HYPERLINK(1)" and rows[2] == ["22222222", "245", "Plain", tenant["slug"]]
        audit = (await db_session.execute(select(AuditLog).where(AuditLog.table_name == "player_exports"))).scalar_one()
        assert audit.after == '{"count": 2}'

    async def test_viewer_cannot_export(self, tenant, make_user_and_client):
        viewer, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")])
        assert (await viewer.get(f"{A}/players/export", headers=_h(tenant))).status_code == 403


class TestPlayersAreNeverPublic:
    async def test_no_public_route_returns_a_player_id(self, client, client_no_session, db_session):
        await _add(client, "11111111,245,Hidden Name")
        for path in ("/api/events", "/events.ics", "/api/alliances", "/api/kingdom-branding", "/api/last-activity", "/events", "/feedback"):
            body = (await client_no_session.get(path)).text
            assert "11111111" not in body and "Hidden Name" not in body, path
        assert (await client_no_session.get(f"{A}/players")).status_code == 401
