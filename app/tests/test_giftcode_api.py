"""Spec §79.6: starting, watching and cancelling gift code runs over HTTP."""
import pytest
from sqlalchemy import select

from models.db import AuditLog, Kingdom
from services import giftcode_engine as engine
from tests.giftcode_helpers import Clock, FakeGiftClient, ticker

A = "/admin/api"
CODE = "KSTEST150"


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("KS_GIFTCODE_SIGN_KEY", "test-key")


async def _roster(client, text="11111111\n22222222", game_number=138, db_session=None, tenant=None):
    if db_session is not None:
        (await db_session.get(Kingdom, tenant["kingdom_id"])).game_number = game_number
        await db_session.commit()
    assert (await client.post(f"{A}/players/bulk", json={"text": text})).status_code == 200


async def test_config_reports_whether_the_feature_is_on(client, monkeypatch):
    monkeypatch.delenv("KS_GIFTCODE_SIGN_KEY", raising=False)
    assert (await client.get(f"{A}/giftcode-config")).json()["enabled"] is False
    monkeypatch.setenv("KS_GIFTCODE_SIGN_KEY", "k")
    assert (await client.get(f"{A}/giftcode-config")).json()["enabled"] is True


async def test_off_without_a_key_is_503(client, tenant, monkeypatch):
    monkeypatch.delenv("KS_GIFTCODE_SIGN_KEY", raising=False)
    assert (await client.post(f"{A}/giftcode-runs", json={"code": CODE, "tenant_ids": [tenant["id"]]})).status_code == 503


async def test_start_watch_and_summarise(client, tenant, db_session, sf, on):
    await _roster(client, "11111111\n22222222\n33333333", db_session=db_session, tenant=tenant)
    r = await client.post(f"{A}/giftcode-runs", json={"code": CODE, "tenant_ids": [tenant["id"]]})
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["status"] == "queued" and run["counts"]["waiting"] == 3
    await engine.run_tick(sf, FakeGiftClient(script={"22222222": ["USER INFO ERROR"], "33333333": ["STOVE_LV ERROR"]}), **ticker(Clock()))
    detail = (await client.get(f"{A}/giftcode-runs/{run['id']}")).json()
    assert detail["status"] == "done"
    assert detail["counts"]["redeemed"] == 1 and detail["counts"]["wrong_kingdom"] == 1 and detail["counts"]["requirement"] == 1
    assert [w["fid"] for w in detail["wrong_kingdom"]] == ["22222222"]
    assert [p["fid"] for p in detail["problems"]] == ["33333333"]
    listing = (await client.get(f"{A}/giftcode-runs")).json()
    assert [r["id"] for r in listing] == [run["id"]]


async def test_a_second_run_while_one_is_active_is_409(client, tenant, db_session, on):
    await _roster(client, db_session=db_session, tenant=tenant)
    body = {"code": CODE, "tenant_ids": [tenant["id"]]}
    assert (await client.post(f"{A}/giftcode-runs", json=body)).status_code == 201
    assert (await client.post(f"{A}/giftcode-runs", json={**body, "code": "OTHER123"})).status_code == 409


async def test_a_missing_kingdom_number_names_the_problem(client, tenant, db_session, on):
    await _roster(client, game_number=None, db_session=db_session, tenant=tenant)
    r = await client.post(f"{A}/giftcode-runs", json={"code": CODE, "tenant_ids": [tenant["id"]]})
    assert r.status_code == 422 and "game number" in r.json()["detail"]


async def test_a_bad_code_is_422(client, tenant, db_session, on):
    await _roster(client, db_session=db_session, tenant=tenant)
    assert (await client.post(f"{A}/giftcode-runs", json={"code": "no way", "tenant_ids": [tenant["id"]]})).status_code == 422


async def test_cancel(client, tenant, db_session, on):
    await _roster(client, db_session=db_session, tenant=tenant)
    run = (await client.post(f"{A}/giftcode-runs", json={"code": CODE, "tenant_ids": [tenant["id"]]})).json()
    out = (await client.post(f"{A}/giftcode-runs/{run['id']}/cancel")).json()
    assert out["status"] == "cancelled" and out["counts"]["skipped"] == 2
    assert (await client.post(f"{A}/giftcode-runs/{run['id']}/cancel")).status_code == 409


async def test_audit_rows_name_the_code_and_a_count_not_players(client, tenant, db_session, on):
    await _roster(client, db_session=db_session, tenant=tenant)
    await client.post(f"{A}/giftcode-runs", json={"code": CODE, "tenant_ids": [tenant["id"]]})
    row = (await db_session.execute(select(AuditLog).where(AuditLog.table_name == "redemption_runs"))).scalar_one()
    assert CODE in row.after and "11111111" not in row.after and '"players": 2' in row.after


class TestPermissions:
    async def test_a_viewer_cannot_start_or_cancel(self, client, tenant, db_session, make_user_and_client, on):
        await _roster(client, db_session=db_session, tenant=tenant)
        viewer, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")])
        assert (await viewer.post(f"{A}/giftcode-runs", json={"code": CODE, "tenant_ids": [tenant["id"]]})).status_code == 403
        run = (await client.post(f"{A}/giftcode-runs", json={"code": CODE, "tenant_ids": [tenant["id"]]})).json()
        assert (await viewer.post(f"{A}/giftcode-runs/{run['id']}/cancel")).status_code == 403

    async def test_cannot_start_for_an_alliance_you_lack(self, client, tenant, second_tenant, db_session, make_user_and_client, on):
        coord, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "coordinator")])
        body = {"code": CODE, "tenant_ids": [tenant["id"], second_tenant["id"]]}
        assert (await coord.post(f"{A}/giftcode-runs", json=body)).status_code == 403

    async def test_a_coordinator_sees_only_their_alliances_part_of_a_shared_run(
            self, client, tenant, second_tenant, db_session, make_user_and_client, on):
        await _roster(client, "11111111", db_session=db_session, tenant=tenant)
        client.headers["X-Tenant-Slug"] = second_tenant["slug"]
        await _roster(client, "22222222")
        run = (await client.post(f"{A}/giftcode-runs", json={"code": CODE, "tenant_ids": [tenant["id"], second_tenant["id"]]})).json()
        other, _ = await make_user_and_client(tenant_grants=[(second_tenant["id"], "viewer")])
        mine = (await other.get(f"{A}/giftcode-runs/{run['id']}", headers={"X-Tenant-Slug": second_tenant["slug"]})).json()
        assert mine["counts"]["total"] == 1
        assert (await other.get(f"{A}/giftcode-runs/{run['id']}", headers={"X-Tenant-Slug": tenant["slug"]})).status_code == 403
        nobody, _ = await make_user_and_client(tenant_grants=[], discord_id="nobody")
        assert (await nobody.get(f"{A}/giftcode-runs/{run['id']}", headers={"X-Tenant-Slug": "*"})).status_code == 403

    async def test_an_unknown_run_is_404(self, client):
        assert (await client.get(f"{A}/giftcode-runs/999")).status_code == 404
        assert (await client.post(f"{A}/giftcode-runs/999/cancel")).status_code == 404


async def test_kingdom_game_number_is_set_by_a_superadmin_and_audited(client, tenant, db_session):
    r = await client.patch(f"{A}/kingdoms/{tenant['kingdom_id']}", json={"game_number": 138})
    assert r.status_code == 200 and r.json()["game_number"] == 138
    assert (await client.patch(f"{A}/kingdoms/{tenant['kingdom_id']}", json={"game_number": None})).json()["game_number"] is None
    assert (await client.patch(f"{A}/kingdoms/{tenant['kingdom_id']}", json={"game_number": 0})).status_code == 422
