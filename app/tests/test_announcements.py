"""Tests for scheduled announcements: creation and its validation,
permission scoping to the creator's own tenant access, the per-minute
delivery job's independent per-target success/failure, and cancellation.
"""
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models.db import Announcement, AnnouncementTarget
from scheduler.announcements import send_scheduled_announcements


def _future_iso(minutes=0):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


async def _run_delivery_job(db_engine):
    """send_scheduled_announcements() now takes an injectable
    session_factory (see scheduler/jobs.py's module docstring) —
    point it at this test's own in-memory engine instead of the real
    database models.AsyncSessionLocal defaults to."""
    TestSessionLocal = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    await send_scheduled_announcements(session_factory=TestSessionLocal)


class TestCreateAnnouncement:

    async def test_create_returns_201(self, client: AsyncClient, tenant: dict):
        r = await client.post("/admin/api/announcements", json={
            "title": "Server maintenance",
            "body_markdown": "**Heads up** — brief downtime tonight.",
            "scheduled_for": _future_iso(),
            "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
        })
        assert r.status_code == 201, r.text
        assert r.json()["status"] == "scheduled"
        assert len(r.json()["targets"]) == 1

    async def test_body_over_2000_chars_rejected(self, client: AsyncClient, tenant: dict):
        r = await client.post("/admin/api/announcements", json={
            "title": "Too long", "body_markdown": "x" * 2001,
            "scheduled_for": _future_iso(),
            "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
        })
        assert r.status_code == 422
        assert "2000" in r.json()["detail"]

    async def test_no_targets_rejected(self, client: AsyncClient):
        r = await client.post("/admin/api/announcements", json={
            "title": "No targets", "body_markdown": "hi", "scheduled_for": _future_iso(), "targets": [],
        })
        assert r.status_code == 422

    async def test_unknown_tenant_slug_rejected(self, client: AsyncClient):
        r = await client.post("/admin/api/announcements", json={
            "title": "Bad target", "body_markdown": "hi", "scheduled_for": _future_iso(),
            "targets": [{"tenant_slug": "nonexistent", "discord_channel_id": "chan-1"}],
        })
        assert r.status_code == 404

    async def test_multi_target_creates_one_row_per_target(
        self, client: AsyncClient, tenant: dict, second_tenant: dict
    ):
        r = await client.post("/admin/api/announcements", json={
            "title": "Kingdom-wide notice", "body_markdown": "Everyone please read this.",
            "scheduled_for": _future_iso(),
            "targets": [
                {"tenant_slug": tenant["slug"], "discord_channel_id": "chan-mod"},
                {"tenant_slug": second_tenant["slug"], "discord_channel_id": "chan-nsr"},
            ],
        })
        assert r.status_code == 201, r.text
        assert len(r.json()["targets"]) == 2


class TestAnnouncementPermissions:

    async def test_cannot_target_tenant_without_access(
        self, make_user_and_client, tenant: dict, second_tenant: dict
    ):
        """A coordinator can only select tenants they hold UserTenant
        access to — matching the invite system's own access list."""
        client, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "coordinator")])
        r = await client.post(
            "/admin/api/announcements",
            json={
                "title": "Sneaky", "body_markdown": "hi", "scheduled_for": _future_iso(),
                "targets": [{"tenant_slug": second_tenant["slug"], "discord_channel_id": "chan-1"}],
            },
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert r.status_code == 403
        await client.aclose()

    async def test_can_target_tenant_with_access(
        self, make_user_and_client, tenant: dict, second_tenant: dict
    ):
        client, _ = await make_user_and_client(
            tenant_grants=[(tenant["id"], "coordinator"), (second_tenant["id"], "coordinator")]
        )
        r = await client.post(
            "/admin/api/announcements",
            json={
                "title": "Legit", "body_markdown": "hi", "scheduled_for": _future_iso(),
                "targets": [
                    {"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"},
                    {"tenant_slug": second_tenant["slug"], "discord_channel_id": "chan-2"},
                ],
            },
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert r.status_code == 201, r.text
        await client.aclose()


class TestAnnouncementDelivery:

    async def test_due_announcement_delivered_and_marked_posted(
        self, client: AsyncClient, db_session, db_engine, tenant: dict, monkeypatch
    ):
        created = await client.post("/admin/api/announcements", json={
            "title": "Now", "body_markdown": "Delivering now",
            "scheduled_for": _future_iso(minutes=5),  # 5 minutes in the past — due
            "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
        })
        announcement_id = created.json()["id"]

        calls = []
        async def fake_send(token, channel_id, message):
            calls.append((channel_id, message))
            return True, ""
        monkeypatch.setattr("scheduler.announcements.send_channel_message", fake_send)

        await _run_delivery_job(db_engine)

        announcement = await db_session.get(Announcement, announcement_id)
        assert announcement.status == "posted"
        assert announcement.posted_at is not None
        assert calls == [("chan-1", "Delivering now")]

    async def test_one_target_failing_does_not_block_or_corrupt_the_other(
        self, client: AsyncClient, db_session, db_engine, tenant: dict, second_tenant: dict, monkeypatch
    ):
        created = await client.post("/admin/api/announcements", json={
            "title": "Partial failure", "body_markdown": "Testing resilience",
            "scheduled_for": _future_iso(minutes=5),
            "targets": [
                {"tenant_slug": tenant["slug"], "discord_channel_id": "chan-mod"},
                {"tenant_slug": second_tenant["slug"], "discord_channel_id": "chan-nsr"},
            ],
        })
        announcement_id = created.json()["id"]

        async def flaky_send(token, channel_id, message):
            if channel_id == "chan-nsr":
                return False, "502 Discord unavailable"
            return True, ""
        monkeypatch.setattr("scheduler.announcements.send_channel_message", flaky_send)

        await _run_delivery_job(db_engine)

        targets = (await db_session.execute(
            select(AnnouncementTarget).where(AnnouncementTarget.announcement_id == announcement_id)
        )).scalars().all()
        by_channel = {t.discord_channel_id: t for t in targets}
        assert by_channel["chan-mod"].post_status == "posted"
        assert by_channel["chan-nsr"].post_status == "error"

        announcement = await db_session.get(Announcement, announcement_id)
        # Overall status is "posted" because at least one target succeeded
        # — matching the same any_posted logic as kingdom-wide event PostLog.
        assert announcement.status == "posted"

    async def test_not_yet_due_announcement_is_not_delivered(
        self, client: AsyncClient, db_session, db_engine, tenant: dict, monkeypatch
    ):
        created = await client.post("/admin/api/announcements", json={
            "title": "Later", "body_markdown": "Not yet",
            "scheduled_for": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
        })
        announcement_id = created.json()["id"]

        calls = []
        async def fake_send(token, channel_id, message):
            calls.append(channel_id)
            return True, ""
        monkeypatch.setattr("scheduler.announcements.send_channel_message", fake_send)

        await _run_delivery_job(db_engine)

        assert calls == []
        announcement = await db_session.get(Announcement, announcement_id)
        assert announcement.status == "scheduled"


class TestCancelAnnouncement:

    async def test_cancel_scheduled_announcement(self, client: AsyncClient, tenant: dict):
        created = await client.post("/admin/api/announcements", json={
            "title": "To cancel", "body_markdown": "hi",
            "scheduled_for": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
        })
        announcement_id = created.json()["id"]

        r = await client.post(f"/admin/api/announcements/{announcement_id}/cancel")
        assert r.status_code == 200

        listed = await client.get("/admin/api/announcements")
        found = next(a for a in listed.json() if a["id"] == announcement_id)
        assert found["status"] == "cancelled"

    async def test_cancelled_announcement_not_delivered(
        self, client: AsyncClient, db_session, db_engine, tenant: dict, monkeypatch
    ):
        created = await client.post("/admin/api/announcements", json={
            "title": "Cancel before due", "body_markdown": "hi",
            "scheduled_for": _future_iso(minutes=5),
            "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
        })
        announcement_id = created.json()["id"]
        await client.post(f"/admin/api/announcements/{announcement_id}/cancel")

        calls = []
        async def fake_send(token, channel_id, message):
            calls.append(channel_id)
            return True, ""
        monkeypatch.setattr("scheduler.announcements.send_channel_message", fake_send)

        await _run_delivery_job(db_engine)
        assert calls == []
