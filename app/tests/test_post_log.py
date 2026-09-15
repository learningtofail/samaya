"""Tests for routers/admin/post_log.py — GET /api/post-log and the CSV
export. Neither had any test coverage before this file; added after a
frontend bug (postlog.js still calling the retired X-Admin-Key auth
model) turned up during the Phase 4/5 code-quality pass specifically
because this endpoint's path had no test exercising it end to end.
"""
from datetime import date, datetime, timezone

import pytest
from httpx import AsyncClient

from models.db import PostLog


async def _seed_post_log(db_session, tenant_id: int, event_name="Weekly Raid", status="posted"):
    log = PostLog(
        tenant_id=tenant_id, event_name=event_name, occurrence_date=date.today(),
        discord_event_id="discord-evt-1", discord_guild_id="test-guild",
        posted_at_utc=datetime.now(timezone.utc), posted_by="coordinator", status=status,
    )
    db_session.add(log)
    await db_session.commit()
    return log


class TestGetPostLog:

    async def test_returns_this_tenants_entries(self, client: AsyncClient, db_session, tenant: dict):
        await _seed_post_log(db_session, tenant["id"])
        r = await client.get("/admin/api/post-log")
        assert r.status_code == 200
        assert len(r.json()) == 1
        assert r.json()[0]["event_name"] == "Weekly Raid"

    async def test_does_not_return_another_tenants_entries(
        self, client: AsyncClient, db_session, tenant: dict, second_tenant: dict
    ):
        await _seed_post_log(db_session, second_tenant["id"], event_name="NSR Only")
        r = await client.get("/admin/api/post-log", headers={"X-Tenant-Slug": tenant["slug"]})
        assert r.json() == []

    async def test_requires_login(self, client_no_session: AsyncClient, tenant: dict):
        r = await client_no_session.get("/admin/api/post-log", headers={"X-Tenant-Slug": tenant["slug"]})
        assert r.status_code == 401


class TestExportPostLogCsv:

    async def test_returns_csv_with_correct_headers(self, client: AsyncClient, db_session, tenant: dict):
        await _seed_post_log(db_session, tenant["id"])
        r = await client.get("/admin/api/post-log/export.csv")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        assert "attachment" in r.headers["content-disposition"]

    async def test_csv_content_includes_seeded_row(self, client: AsyncClient, db_session, tenant: dict):
        await _seed_post_log(db_session, tenant["id"], event_name="Weekly Raid")
        r = await client.get("/admin/api/post-log/export.csv")
        assert "Weekly Raid" in r.text
        assert "discord-evt-1" in r.text

    async def test_does_not_include_another_tenants_rows(
        self, client: AsyncClient, db_session, tenant: dict, second_tenant: dict
    ):
        await _seed_post_log(db_session, second_tenant["id"], event_name="NSR Only Event")
        r = await client.get("/admin/api/post-log/export.csv", headers={"X-Tenant-Slug": tenant["slug"]})
        assert "NSR Only Event" not in r.text

    async def test_requires_login(self, client_no_session: AsyncClient, tenant: dict):
        r = await client_no_session.get(
            "/admin/api/post-log/export.csv", headers={"X-Tenant-Slug": tenant["slug"]}
        )
        assert r.status_code == 401

    async def test_requires_tenant_access(self, make_user_and_client, tenant: dict):
        client, _ = await make_user_and_client(tenant_grants=[])
        r = await client.get(
            "/admin/api/post-log/export.csv", headers={"X-Tenant-Slug": tenant["slug"]}
        )
        assert r.status_code == 403
        await client.aclose()
