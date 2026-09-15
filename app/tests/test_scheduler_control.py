"""Tests for routers/admin/scheduler_control.py's manual regenerate
endpoint. Had zero coverage before this file — added after this exact
endpoint turned up broken during the Phase 4/5 code-quality pass: it
called scheduler/regeneration.py's regenerate_occurrences() without
threading a session_factory through, so it always used the real
AsyncSessionLocal regardless of any request-scoped DB override (see
models/__init__.py's get_session_factory docstring for the actual fix).
Preview is separately covered in test_routes_events.py.
"""
from datetime import date, time

from httpx import AsyncClient
from sqlalchemy import select

from models.db import EventDefinition, Occurrence


class TestManualRegenerate:

    async def test_returns_ok(self, client: AsyncClient):
        r = await client.post("/admin/api/scheduler/regenerate")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    async def test_actually_generates_occurrences_for_this_tenants_events(
        self, client: AsyncClient, db_session, tenant: dict
    ):
        event = EventDefinition(
            owning_tenant_id=tenant["id"], scope="alliance", name="Weekly Raid",
            interval_days=7, start_time_utc=time(19, 0), duration_hours=1.0,
            anchor_date=date.today(), notification_channel_id="", notification_role_id="",
        )
        db_session.add(event)
        await db_session.commit()

        r = await client.post("/admin/api/scheduler/regenerate")
        assert r.status_code == 200

        occs = (await db_session.execute(
            select(Occurrence).where(Occurrence.tenant_id == tenant["id"])
        )).scalars().all()
        assert len(occs) > 0

    async def test_does_not_touch_another_tenants_occurrences(
        self, client: AsyncClient, db_session, tenant: dict, second_tenant: dict
    ):
        nsr_event = EventDefinition(
            owning_tenant_id=second_tenant["id"], scope="alliance", name="NSR Practice",
            interval_days=7, start_time_utc=time(20, 0), duration_hours=1.0,
            anchor_date=date.today(), notification_channel_id="", notification_role_id="",
        )
        db_session.add(nsr_event)
        await db_session.commit()

        # Regenerate MOD only
        r = await client.post("/admin/api/scheduler/regenerate", headers={"X-Tenant-Slug": tenant["slug"]})
        assert r.status_code == 200

        nsr_occs = (await db_session.execute(
            select(Occurrence).where(Occurrence.tenant_id == second_tenant["id"])
        )).scalars().all()
        assert nsr_occs == []
