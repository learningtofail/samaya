"""
Integration tests for /admin/api/events endpoints.
Uses the test database via the client fixture from conftest.py.
"""
import pytest
from httpx import AsyncClient


VALID_EVENT = {
    "name":            "Viking Vengeance",
    "interval_days":   14,
    "start_time_utc":  "20:00",
    "duration_hours":  1.0,
    "discord_channel": "#alliance-war",
    "description":     "Biweekly alliance PvP",
    "category":        "Alliance",
    "anchor_date":     "2025-05-04",
}


async def create_event(client: AsyncClient, overrides=None) -> dict:
    data = {**VALID_EVENT, **(overrides or {})}
    r = await client.post("/admin/api/events", json=data)
    assert r.status_code == 201, r.text
    return r.json()


class TestCreateEvent:

    async def test_create_returns_201(self, client: AsyncClient):
        r = await client.post("/admin/api/events", json=VALID_EVENT)
        assert r.status_code == 201

    async def test_create_returns_event_with_id(self, client: AsyncClient):
        r = await client.post("/admin/api/events", json=VALID_EVENT)
        data = r.json()
        assert "id" in data
        assert data["id"] > 0

    async def test_create_stores_all_fields(self, client: AsyncClient):
        r = await client.post("/admin/api/events", json=VALID_EVENT)
        data = r.json()
        assert data["name"]           == VALID_EVENT["name"]
        assert data["interval_days"]  == VALID_EVENT["interval_days"]
        assert data["start_time_utc"] == VALID_EVENT["start_time_utc"]
        assert data["duration_hours"] == VALID_EVENT["duration_hours"]
        assert data["anchor_date"]    == VALID_EVENT["anchor_date"]

    async def test_create_sets_active_true_by_default(self, client: AsyncClient):
        r = await client.post("/admin/api/events", json=VALID_EVENT)
        assert r.json()["active"] is True

    async def test_create_normalises_time_format(self, client: AsyncClient):
        data = {**VALID_EVENT, "start_time_utc": "9:00"}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 201
        assert r.json()["start_time_utc"] == "09:00"


class TestListEvents:

    async def test_empty_list_on_fresh_db(self, client: AsyncClient):
        r = await client.get("/admin/api/events")
        assert r.status_code == 200
        assert r.json() == []

    async def test_created_event_appears_in_list(self, client: AsyncClient):
        await create_event(client)
        r = await client.get("/admin/api/events")
        assert len(r.json()) == 1

    async def test_multiple_events_all_appear(self, client: AsyncClient):
        await create_event(client, {"name": "Event A"})
        await create_event(client, {"name": "Event B"})
        r = await client.get("/admin/api/events")
        assert len(r.json()) == 2

    async def test_list_includes_inactive_events(self, client: AsyncClient):
        event = await create_event(client)
        await client.patch(f"/admin/api/events/{event['id']}", json={"active": False})
        r = await client.get("/admin/api/events")
        assert len(r.json()) == 1
        assert r.json()[0]["active"] is False


class TestUpdateEvent:

    async def test_patch_name(self, client: AsyncClient):
        event = await create_event(client)
        r = await client.patch(f"/admin/api/events/{event['id']}", json={"name": "Updated Name"})
        assert r.status_code == 200
        assert r.json()["name"] == "Updated Name"

    async def test_patch_interval(self, client: AsyncClient):
        event = await create_event(client)
        r = await client.patch(f"/admin/api/events/{event['id']}", json={"interval_days": 28})
        assert r.status_code == 200
        assert r.json()["interval_days"] == 28

    async def test_deactivate_event(self, client: AsyncClient):
        event = await create_event(client)
        r = await client.patch(f"/admin/api/events/{event['id']}", json={"active": False})
        assert r.status_code == 200
        assert r.json()["active"] is False

    async def test_patch_nonexistent_returns_404(self, client: AsyncClient):
        r = await client.patch("/admin/api/events/9999", json={"name": "X"})
        assert r.status_code == 404

    async def test_patch_invalid_interval_returns_422(self, client: AsyncClient):
        event = await create_event(client)
        r = await client.patch(f"/admin/api/events/{event['id']}", json={"interval_days": 0})
        assert r.status_code == 422


class TestSoftDelete:

    async def test_delete_sets_inactive(self, client: AsyncClient):
        event = await create_event(client)
        r = await client.delete(f"/admin/api/events/{event['id']}")
        assert r.status_code == 204
        r2 = await client.get("/admin/api/events")
        assert r2.json()[0]["active"] is False

    async def test_delete_nonexistent_returns_404(self, client: AsyncClient):
        r = await client.delete("/admin/api/events/9999")
        assert r.status_code == 404


class TestPermanentDelete:

    async def test_permanent_delete_removes_event(self, client: AsyncClient):
        event = await create_event(client)
        r = await client.delete(f"/admin/api/events/{event['id']}/permanent")
        assert r.status_code == 200
        r2 = await client.get("/admin/api/events")
        assert r2.json() == []

    async def test_permanent_delete_returns_event_name(self, client: AsyncClient):
        event = await create_event(client)
        r = await client.delete(f"/admin/api/events/{event['id']}/permanent")
        assert r.json()["event_name"] == VALID_EVENT["name"]

    async def test_permanent_delete_nonexistent_returns_404(self, client: AsyncClient):
        r = await client.delete("/admin/api/events/9999/permanent")
        assert r.status_code == 404


class TestPreview:

    async def test_preview_returns_10_dates(self, client: AsyncClient):
        r = await client.post("/admin/api/scheduler/preview", json=VALID_EVENT)
        assert r.status_code == 200
        assert len(r.json()) == 10

    async def test_preview_dates_are_sorted(self, client: AsyncClient):
        r = await client.post("/admin/api/scheduler/preview", json=VALID_EVENT)
        dates = [item["date"] for item in r.json()]
        assert dates == sorted(dates)

    async def test_preview_includes_day_name(self, client: AsyncClient):
        r = await client.post("/admin/api/scheduler/preview", json=VALID_EVENT)
        for item in r.json():
            assert "day" in item
            assert item["day"] in [
                "Monday","Tuesday","Wednesday","Thursday",
                "Friday","Saturday","Sunday"
            ]


class TestAllianceField:

    async def test_create_defaults_to_server(self, client: AsyncClient):
        r = await client.post("/admin/api/events", json=VALID_EVENT)
        assert r.json()["alliance"] == "Server"

    async def test_create_accepts_m0d(self, client: AsyncClient):
        data = {**VALID_EVENT, "alliance": "M0D"}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 201
        assert r.json()["alliance"] == "M0D"

    async def test_create_accepts_nsr(self, client: AsyncClient):
        data = {**VALID_EVENT, "alliance": "NSR"}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 201
        assert r.json()["alliance"] == "NSR"

    async def test_create_rejects_invalid_alliance(self, client: AsyncClient):
        data = {**VALID_EVENT, "alliance": "PvE"}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 422
        assert "alliance" in r.json()["detail"].lower()

    async def test_patch_alliance(self, client: AsyncClient):
        event = await create_event(client)
        r = await client.patch(f"/admin/api/events/{event['id']}", json={"alliance": "M0D"})
        assert r.status_code == 200
        assert r.json()["alliance"] == "M0D"

    async def test_patch_rejects_invalid_alliance(self, client: AsyncClient):
        event = await create_event(client)
        r = await client.patch(f"/admin/api/events/{event['id']}", json={"alliance": "Cycle"})
        assert r.status_code == 422


class TestLeadershipOnlyField:

    async def test_create_defaults_to_false(self, client: AsyncClient):
        r = await client.post("/admin/api/events", json=VALID_EVENT)
        assert r.json()["leadership_only"] is False

    async def test_create_accepts_true(self, client: AsyncClient):
        data = {**VALID_EVENT, "leadership_only": True}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 201
        assert r.json()["leadership_only"] is True

    async def test_patch_leadership_only(self, client: AsyncClient):
        event = await create_event(client)
        r = await client.patch(f"/admin/api/events/{event['id']}", json={"leadership_only": True})
        assert r.status_code == 200
        assert r.json()["leadership_only"] is True

    async def test_patch_leadership_only_false_explicitly(self, client: AsyncClient):
        event = await create_event(client, {"leadership_only": True})
        r = await client.patch(f"/admin/api/events/{event['id']}", json={"leadership_only": False})
        assert r.status_code == 200
        assert r.json()["leadership_only"] is False
