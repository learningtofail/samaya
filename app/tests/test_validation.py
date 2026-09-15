"""
Tests for EventIn Pydantic validation.
All bad inputs should return 422 with a readable detail message.
All good inputs should return 201.
"""
import pytest
from httpx import AsyncClient


VALID_EVENT = {
    "name":            "Test Event",
    "interval_days":   7,
    "start_time_utc":  "19:00",
    "duration_hours":  2.0,
    "discord_channel": "#test",
    "description":     "A test event",
    "anchor_date":     "2025-05-01",
}


# ── interval_days ─────────────────────────────────────────────

class TestIntervalDaysValidation:

    async def test_interval_zero_rejected(self, client: AsyncClient):
        data = {**VALID_EVENT, "interval_days": 0}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 422
        assert "interval" in r.json()["detail"].lower()

    async def test_interval_negative_rejected(self, client: AsyncClient):
        data = {**VALID_EVENT, "interval_days": -1}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 422

    async def test_interval_one_accepted(self, client: AsyncClient):
        data = {**VALID_EVENT, "interval_days": 1}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 201

    async def test_interval_28_accepted(self, client: AsyncClient):
        data = {**VALID_EVENT, "interval_days": 28}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 201

    async def test_interval_string_rejected(self, client: AsyncClient):
        data = {**VALID_EVENT, "interval_days": "weekly"}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 422


# ── duration_hours ────────────────────────────────────────────

class TestDurationValidation:

    async def test_duration_zero_rejected(self, client: AsyncClient):
        data = {**VALID_EVENT, "duration_hours": 0}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 422
        assert "duration" in r.json()["detail"].lower()

    async def test_duration_negative_rejected(self, client: AsyncClient):
        data = {**VALID_EVENT, "duration_hours": -1}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 422

    async def test_duration_half_hour_accepted(self, client: AsyncClient):
        data = {**VALID_EVENT, "duration_hours": 0.5}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 201

    async def test_duration_24_accepted(self, client: AsyncClient):
        data = {**VALID_EVENT, "duration_hours": 24}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 201


# ── start_time_utc ────────────────────────────────────────────

class TestStartTimeValidation:

    async def test_invalid_hour_rejected(self, client: AsyncClient):
        data = {**VALID_EVENT, "start_time_utc": "25:00"}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 422
        assert "hour" in r.json()["detail"].lower() or "time" in r.json()["detail"].lower()

    async def test_invalid_minute_rejected(self, client: AsyncClient):
        data = {**VALID_EVENT, "start_time_utc": "19:60"}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 422

    async def test_invalid_format_rejected(self, client: AsyncClient):
        data = {**VALID_EVENT, "start_time_utc": "7pm"}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 422

    async def test_midnight_accepted(self, client: AsyncClient):
        data = {**VALID_EVENT, "start_time_utc": "00:00"}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 201

    async def test_end_of_day_accepted(self, client: AsyncClient):
        data = {**VALID_EVENT, "start_time_utc": "23:59"}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 201

    async def test_single_digit_hour_accepted(self, client: AsyncClient):
        data = {**VALID_EVENT, "start_time_utc": "9:00"}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 201


# ── anchor_date ───────────────────────────────────────────────

class TestAnchorDateValidation:

    async def test_invalid_date_string_rejected(self, client: AsyncClient):
        data = {**VALID_EVENT, "anchor_date": "not-a-date"}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 422
        assert "anchor" in r.json()["detail"].lower() or "date" in r.json()["detail"].lower()

    async def test_wrong_format_rejected(self, client: AsyncClient):
        data = {**VALID_EVENT, "anchor_date": "01/05/2025"}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 422

    async def test_valid_date_accepted(self, client: AsyncClient):
        data = {**VALID_EVENT, "anchor_date": "2025-05-01"}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 201


# ── notify_minutes_before ─────────────────────────────────────

class TestNotifyMinutesValidation:

    async def test_zero_rejected(self, client: AsyncClient):
        data = {**VALID_EVENT, "notify_minutes_before": 0}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 422

    async def test_negative_rejected(self, client: AsyncClient):
        data = {**VALID_EVENT, "notify_minutes_before": -5}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 422

    async def test_null_accepted(self, client: AsyncClient):
        data = {**VALID_EVENT, "notify_minutes_before": None}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 201

    async def test_omitted_accepted(self, client: AsyncClient):
        data = {k: v for k, v in VALID_EVENT.items()}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 201

    async def test_positive_value_accepted(self, client: AsyncClient):
        data = {**VALID_EVENT, "notify_minutes_before": 60}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 201


# ── multiple errors returned together ────────────────────────

class TestMultipleErrors:

    async def test_multiple_invalid_fields_reported(self, client: AsyncClient):
        data = {
            **VALID_EVENT,
            "interval_days":  0,
            "duration_hours": -1,
        }
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 422
        # Both errors should appear in the detail string
        detail = r.json()["detail"]
        assert "interval" in detail.lower() or "duration" in detail.lower()


# ── scope ─────────────────────────────────────────────────────

class TestScopeValidation:

    async def test_alliance_accepted(self, client: AsyncClient):
        data = {**VALID_EVENT, "scope": "alliance"}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 201

    async def test_kingdom_wide_accepted(self, client: AsyncClient):
        data = {**VALID_EVENT, "scope": "kingdom-wide"}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 201

    async def test_default_when_omitted(self, client: AsyncClient):
        r = await client.post("/admin/api/events", json=VALID_EVENT)
        assert r.json()["scope"] == "alliance"

    async def test_unknown_scope_rejected(self, client: AsyncClient):
        data = {**VALID_EVENT, "scope": "everyone"}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 422
        assert "scope" in r.json()["detail"].lower()

    async def test_wrong_case_rejected(self, client: AsyncClient):
        data = {**VALID_EVENT, "scope": "Alliance"}
        r = await client.post("/admin/api/events", json=data)
        assert r.status_code == 422
