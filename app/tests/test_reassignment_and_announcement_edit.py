"""Spec §49 — reassigning an EventDefinition/Announcement to a different
owning alliance via the merged "Owning Alliance" selector's
owning_tenant_slug field, and editing an already-scheduled Announcement in
place (previously only Cancel/Delete/Duplicate were available)."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models.db import Announcement, DiscordServer, EventDefinition, Tenant


def _future_iso(minutes=60):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


async def _create_event(client, tenant_slug, name="Test Event", **overrides):
    payload = {
        "name": name, "interval_days": 7, "start_time_utc": "19:00",
        "duration_hours": 1.5, "discord_channel": "general", "description": "",
        "anchor_date": "2026-01-05",
        "notification_channel_id": "chan-1", "notification_role_id": "role-1",
        "targets": [],
    }
    payload.update(overrides)
    resp = await client.post("/admin/api/events", json=payload, headers={"X-Tenant-Slug": tenant_slug})
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestEventReassignment:
    @pytest.mark.asyncio
    async def test_reassign_to_different_server_clears_discord_fields(self, client, tenant, second_tenant):
        event = await _create_event(client, tenant["slug"])
        resp = await client.patch(
            f"/admin/api/events/{event['id']}",
            json={"owning_tenant_slug": second_tenant["slug"]},
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["owning_tenant_id"] == second_tenant["id"]
        # tenant and second_tenant each get their own DiscordServer in
        # conftest.py — different servers, so the old guild's channel/role
        # IDs must not silently carry over.
        assert body["discord_channel"] == ""
        assert body["notification_channel_id"] == ""
        assert body["notification_role_id"] == ""

    @pytest.mark.asyncio
    async def test_reassign_within_shared_server_keeps_discord_fields(self, client, tenant, db_engine):
        """Two tenants sharing one DiscordServer (spec §25) — reassigning
        between them doesn't invalidate the channel/role IDs, since
        they're the same guild either way."""
        TestSessionLocal = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        async with TestSessionLocal() as session:
            result = await session.execute(select(Tenant).where(Tenant.id == tenant["id"]))
            shared_server_id = result.scalar_one().server_id
            sibling = Tenant(kingdom_id=tenant["kingdom_id"], server_id=shared_server_id, name="Sibling", slug="sibling")
            session.add(sibling)
            await session.commit()
            sibling_slug = sibling.slug

        event = await _create_event(client, tenant["slug"])
        resp = await client.patch(
            f"/admin/api/events/{event['id']}",
            json={"owning_tenant_slug": sibling_slug},
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["discord_channel"] == "general"
        assert body["notification_channel_id"] == "chan-1"
        assert body["notification_role_id"] == "role-1"

    @pytest.mark.asyncio
    async def test_reassign_requires_access_to_new_tenant(self, client, tenant, second_tenant, make_user_and_client):
        event = await _create_event(client, tenant["slug"])
        other_client, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "owner")])
        async with other_client as oc:
            resp = await oc.patch(
                f"/admin/api/events/{event['id']}",
                json={"owning_tenant_slug": second_tenant["slug"]},
                headers={"X-Tenant-Slug": tenant["slug"]},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_reassign_to_kingdom_wide_requires_kingdom_coordinator(self, client, tenant, second_tenant, make_user_and_client):
        event = await _create_event(client, tenant["slug"])
        # Access to both alliances, but no UserKingdom grant.
        other_client, _ = await make_user_and_client(
            tenant_grants=[(tenant["id"], "owner"), (second_tenant["id"], "owner")],
        )
        async with other_client as oc:
            resp = await oc.patch(
                f"/admin/api/events/{event['id']}",
                json={"owning_tenant_slug": second_tenant["slug"], "scope": "kingdom-wide"},
                headers={"X-Tenant-Slug": tenant["slug"]},
            )
        assert resp.status_code == 403


class TestAnnouncementEdit:
    @pytest.mark.asyncio
    async def test_edit_scheduled_announcement(self, client, tenant):
        create_resp = await client.post(
            "/admin/api/announcements",
            json={
                "title": "Original", "body_markdown": "hello",
                "scheduled_for": _future_iso(60),
                "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "111"}],
            },
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert create_resp.status_code == 201, create_resp.text
        ann_id = create_resp.json()["id"]

        patch_resp = await client.patch(
            f"/admin/api/announcements/{ann_id}",
            json={"title": "Updated title", "scheduled_for": _future_iso(120)},
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert patch_resp.status_code == 200, patch_resp.text
        body = patch_resp.json()
        assert body["title"] == "Updated title"

    @pytest.mark.asyncio
    async def test_cannot_edit_posted_announcement(self, client, tenant, db_engine):
        create_resp = await client.post(
            "/admin/api/announcements",
            json={
                "title": "Original", "body_markdown": "hello",
                "scheduled_for": _future_iso(60),
                "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "111"}],
            },
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        ann_id = create_resp.json()["id"]

        TestSessionLocal = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        async with TestSessionLocal() as session:
            result = await session.execute(select(Announcement).where(Announcement.id == ann_id))
            ann = result.scalar_one()
            ann.status = "posted"
            await session.commit()

        patch_resp = await client.patch(
            f"/admin/api/announcements/{ann_id}",
            json={"title": "Too late"},
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert patch_resp.status_code == 400

    @pytest.mark.asyncio
    async def test_create_with_scope_and_reassign_owner(self, client, tenant, second_tenant):
        create_resp = await client.post(
            "/admin/api/announcements",
            json={
                "title": "Original", "body_markdown": "hello",
                "scheduled_for": _future_iso(60), "scope": "alliance",
                "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "111"}],
            },
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert create_resp.status_code == 201, create_resp.text
        assert create_resp.json()["scope"] == "alliance"
        ann_id = create_resp.json()["id"]

        patch_resp = await client.patch(
            f"/admin/api/announcements/{ann_id}",
            json={"owning_tenant_slug": second_tenant["slug"]},
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert patch_resp.status_code == 200, patch_resp.text
        assert patch_resp.json()["owning_tenant_id"] == second_tenant["id"]
