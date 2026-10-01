"""Field validation on event creation (POST /admin/api/events). Bad input
must be a 422 whose message names the offending field, not merely any 422:
a missing `type_id` alone would also give 422, so every rejection test
sends an otherwise valid body and checks the field name in the detail."""
import pytest
from httpx import AsyncClient

BASE = "/admin/api"


@pytest.fixture
async def valid_event(client: AsyncClient) -> dict:
    resp = await client.post(f"{BASE}/event-types", json={"name": "Validation"})
    assert resp.status_code == 201, resp.text
    return {
        "type_id": resp.json()["id"], "name": "Test Event", "interval_days": 7,
        "start_time_utc": "19:00", "duration_hours": 2.0, "anchor_date": "2026-05-01",
    }


async def _create(client: AsyncClient, body: dict):
    return await client.post(f"{BASE}/events", json=body)


@pytest.mark.parametrize("field,value", [
    ("interval_days", 0), ("interval_days", -1), ("interval_days", "weekly"),
    ("duration_hours", 0), ("duration_hours", -1),
    ("start_time_utc", "25:00"), ("start_time_utc", "12:60"), ("start_time_utc", "noon"),
    ("anchor_date", "2026-02-30"), ("anchor_date", "05/01/2026"),
    ("scope", "everyone"), ("scope", "Alliance"),
    ("reminder_minutes", [-5]),
    ("recurrence_kind", "monthly"),
])
async def test_invalid_value_is_rejected_and_names_the_field(client, valid_event, field, value):
    resp = await _create(client, {**valid_event, field: value})
    assert resp.status_code == 422, resp.text
    assert field in resp.json()["detail"], resp.json()["detail"]


@pytest.mark.parametrize("overrides", [
    {"interval_days": 1}, {"interval_days": 28},
    {"duration_hours": 0.5}, {"duration_hours": 24},
    {"start_time_utc": "00:00"}, {"start_time_utc": "23:59"}, {"start_time_utc": "9:05"},
    {"scope": "alliance"}, {"scope": "kingdom-wide"},
    {"reminder_minutes": []}, {"reminder_minutes": [0]}, {"reminder_minutes": [60, 10]},
    {"duration_hours": None}, {"recurrence_kind": "none", "interval_days": None},
])
async def test_valid_value_is_accepted(client, valid_event, overrides):
    resp = await _create(client, {**valid_event, **overrides})
    assert resp.status_code == 201, resp.text


async def test_scope_defaults_to_alliance(client, valid_event):
    resp = await _create(client, valid_event)
    assert resp.json()["scope"] == "alliance"


async def test_multiple_invalid_fields_are_all_reported(client, valid_event):
    resp = await _create(client, {**valid_event, "interval_days": 0, "start_time_utc": "99:99"})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "interval_days" in detail and "start_time_utc" in detail
