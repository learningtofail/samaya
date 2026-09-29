"""Tests for GET /admin/api/audit-log (spec §31). Rather than seed
AuditLog rows directly, most of these drive a real mutation through an
existing endpoint that already calls services/audit.log_change (e.g.
creating an invite) and then reads it back — that's what actually proves
the wiring works end-to-end, not just that the query is correct.
"""
from datetime import datetime, timezone

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import AuditLog


class TestAuditLog:

    async def test_owner_action_appears_in_audit_log(self, client: AsyncClient, tenant: dict):
        await client.post("/admin/api/invites", json={"role": "coordinator"})
        r = await client.get("/admin/api/audit-log")
        assert r.status_code == 200
        entries = r.json()
        assert any(e["table_name"] == "invites" and e["action"] == "create" for e in entries)

    async def test_before_after_are_parsed_json(self, client: AsyncClient, tenant: dict):
        await client.post("/admin/api/invites", json={"role": "coordinator"})
        r = await client.get("/admin/api/audit-log")
        entry = next(e for e in r.json() if e["table_name"] == "invites")
        assert entry["before"] is None
        assert entry["after"]["role"] == "coordinator"

    async def test_ordered_newest_first(self, client: AsyncClient, tenant: dict):
        await client.post("/admin/api/invites", json={"role": "coordinator"})
        await client.post("/admin/api/invites", json={"role": "owner"})
        r = await client.get("/admin/api/audit-log")
        timestamps = [e["timestamp"] for e in r.json()]
        assert timestamps == sorted(timestamps, reverse=True)

    async def test_scoped_to_current_tenant(
        self, client: AsyncClient, tenant: dict, second_tenant: dict
    ):
        await client.post(
            "/admin/api/invites", json={"role": "coordinator"},
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        r = await client.get("/admin/api/audit-log", headers={"X-Tenant-Slug": second_tenant["slug"]})
        assert r.json() == []

    async def test_requires_login(self, client_no_session: AsyncClient, tenant: dict):
        r = await client_no_session.get("/admin/api/audit-log", headers={"X-Tenant-Slug": tenant["slug"]})
        assert r.status_code == 401

    async def test_coordinator_cannot_view_audit_log(self, make_user_and_client, tenant: dict):
        client, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "coordinator")])
        r = await client.get("/admin/api/audit-log", headers={"X-Tenant-Slug": tenant["slug"]})
        assert r.status_code == 403
        await client.aclose()

    async def test_owner_can_view_audit_log(self, make_user_and_client, tenant: dict):
        client, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "owner")])
        r = await client.get("/admin/api/audit-log", headers={"X-Tenant-Slug": tenant["slug"]})
        assert r.status_code == 200
        await client.aclose()

    async def test_user_column_resolves_to_discord_username(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict
    ):
        await client.post("/admin/api/invites", json={"role": "coordinator"})
        r = await client.get("/admin/api/audit-log")
        entry = next(e for e in r.json() if e["table_name"] == "invites")
        assert entry["user"] == "TestSuperadmin"

    async def test_system_action_with_no_user_shows_null(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict
    ):
        db_session.add(AuditLog(
            tenant_id=tenant["id"], user_id=None, table_name="occurrences", row_id=1,
            action="update", before=None, after=None, timestamp=datetime.now(timezone.utc),
        ))
        await db_session.commit()
        r = await client.get("/admin/api/audit-log")
        entry = next(e for e in r.json() if e["table_name"] == "occurrences")
        assert entry["user"] is None
