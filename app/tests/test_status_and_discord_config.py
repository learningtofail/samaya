"""Tests for routers/admin/status.py and routers/admin/discord_config.py.
Both had zero test coverage before this file — confirmed not currently
broken via a manual smoke test during the Phase 4/5 code-quality pass,
but "I checked once by hand" isn't the same as having a real test, so
these lock in what that smoke test checked.
"""
from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import Announcement, AnnouncementTarget, PostLog


class TestStatus:
    """Spec §38.2: GET /api/status returns one "alliances" row per
    accessible tenant (a single-slug request still returns a one-item
    list, so callers have one shape to handle either way)."""

    async def test_returns_ok_shape(self, client: AsyncClient, tenant: dict):
        r = await client.get("/admin/api/status")
        assert r.status_code == 200
        body = r.json()
        assert body["service"] == "samaya"
        assert len(body["alliances"]) == 1
        row = body["alliances"][0]
        assert row["tenant_slug"] == tenant["slug"]
        assert "scheduler" in row
        assert "regenerate_occurrences" in row["scheduler"]
        assert "pre_event_notifier" in row["scheduler"]

    async def test_requires_login(self, client_no_session: AsyncClient, tenant: dict):
        r = await client_no_session.get("/admin/api/status", headers={"X-Tenant-Slug": tenant["slug"]})
        assert r.status_code == 401

    async def test_scoped_to_current_tenant(
        self, client: AsyncClient, tenant: dict, second_tenant: dict
    ):
        r1 = await client.get("/admin/api/status", headers={"X-Tenant-Slug": tenant["slug"]})
        r2 = await client.get("/admin/api/status", headers={"X-Tenant-Slug": second_tenant["slug"]})
        assert [a["tenant_slug"] for a in r1.json()["alliances"]] == [tenant["slug"]]
        assert [a["tenant_slug"] for a in r2.json()["alliances"]] == [second_tenant["slug"]]

    async def test_combined_mode_returns_every_accessible_alliance(
        self, client: AsyncClient, tenant: dict, second_tenant: dict
    ):
        r = await client.get("/admin/api/status", headers={"X-Tenant-Slug": "*"})
        slugs = {a["tenant_slug"] for a in r.json()["alliances"]}
        assert {tenant["slug"], second_tenant["slug"]} <= slugs


class TestDeliveryHealth:
    """spec §31 — GET /api/delivery-health, a trailing-7-day rollup of
    AnnouncementTarget and PostLog outcomes for the current tenant."""

    async def test_empty_when_no_activity(self, client: AsyncClient, tenant: dict):
        r = await client.get("/admin/api/delivery-health")
        assert r.status_code == 200
        body = r.json()
        assert body["window_days"] == 7
        assert len(body["alliances"]) == 1
        row = body["alliances"][0]
        assert row["tenant_slug"] == tenant["slug"]
        assert row["announcements"] == {"posted": 0, "error": 0}
        assert row["event_posts"] == {"posted": 0, "error": 0, "cancelled": 0}

    async def test_counts_announcement_targets_within_window(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict
    ):
        # Two separate Announcements, one target apiece — uq_announcement_target
        # is (announcement_id, tenant_id), so one announcement can't carry two
        # targets for the same tenant.
        now = datetime.now(timezone.utc)
        ann1 = Announcement(
            owning_tenant_id=tenant["id"], title="A", body_markdown="hi",
            scheduled_for=now - timedelta(days=1), status="posted", posted_at=now - timedelta(days=1),
        )
        ann2 = Announcement(
            owning_tenant_id=tenant["id"], title="B", body_markdown="hi",
            scheduled_for=now - timedelta(days=1), status="failed", posted_at=now - timedelta(days=1),
        )
        db_session.add_all([ann1, ann2])
        await db_session.commit()
        db_session.add_all([
            AnnouncementTarget(announcement_id=ann1.id, tenant_id=tenant["id"], discord_channel_id="c1", post_status="posted"),
            AnnouncementTarget(announcement_id=ann2.id, tenant_id=tenant["id"], discord_channel_id="c2", post_status="error"),
        ])
        await db_session.commit()

        r = await client.get("/admin/api/delivery-health")
        assert r.json()["alliances"][0]["announcements"] == {"posted": 1, "error": 1}

    async def test_excludes_announcement_targets_outside_window(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict
    ):
        now = datetime.now(timezone.utc)
        ann = Announcement(
            owning_tenant_id=tenant["id"], title="Old", body_markdown="hi",
            scheduled_for=now - timedelta(days=10), status="posted", posted_at=now - timedelta(days=10),
        )
        db_session.add(ann)
        await db_session.commit()
        db_session.add(
            AnnouncementTarget(announcement_id=ann.id, tenant_id=tenant["id"], discord_channel_id="c1", post_status="posted")
        )
        await db_session.commit()

        r = await client.get("/admin/api/delivery-health")
        assert r.json()["alliances"][0]["announcements"] == {"posted": 0, "error": 0}

    async def test_counts_post_log_within_window(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict
    ):
        now = datetime.now(timezone.utc)
        db_session.add_all([
            PostLog(
                tenant_id=tenant["id"], event_name="Siege", occurrence_date=now.date(),
                discord_guild_id="g", posted_at_utc=now - timedelta(hours=1), status="posted",
            ),
            PostLog(
                tenant_id=tenant["id"], event_name="Siege2", occurrence_date=now.date(),
                discord_guild_id="g", posted_at_utc=now - timedelta(hours=2), status="error",
            ),
        ])
        await db_session.commit()

        r = await client.get("/admin/api/delivery-health")
        row = r.json()["alliances"][0]
        assert row["event_posts"]["posted"] == 1
        assert row["event_posts"]["error"] == 1

    async def test_scoped_to_current_tenant(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, second_tenant: dict
    ):
        now = datetime.now(timezone.utc)
        db_session.add(PostLog(
            tenant_id=second_tenant["id"], event_name="Siege", occurrence_date=now.date(),
            discord_guild_id="g", posted_at_utc=now, status="posted",
        ))
        await db_session.commit()

        r = await client.get("/admin/api/delivery-health", headers={"X-Tenant-Slug": tenant["slug"]})
        assert r.json()["alliances"][0]["event_posts"] == {"posted": 0, "error": 0, "cancelled": 0}


class TestDiscordConfig:

    async def test_channels_reflects_discord_api_error(self, client: AsyncClient, monkeypatch):
        """No real Discord bot to call in tests — verifying the error
        path (a fake/invalid token) is what's actually checkable here;
        the success path is exercised implicitly by
        test_post_occurrence.py's monkeypatched create_discord_event
        tests, which cover the same underlying Discord-call pattern."""
        async def fake_channels(token, guild_id):
            return [], "401 Unauthorized"
        monkeypatch.setattr("routers.admin.discord_config.get_guild_channels", fake_channels)
        r = await client.get("/admin/api/discord/channels")
        assert r.status_code == 502

    async def test_roles_reflects_discord_api_error(self, client: AsyncClient, monkeypatch):
        async def fake_roles(token, guild_id):
            return [], "401 Unauthorized"
        monkeypatch.setattr("routers.admin.discord_config.get_guild_roles", fake_roles)
        r = await client.get("/admin/api/discord/roles")
        assert r.status_code == 502

    async def test_channels_success_path(self, client: AsyncClient, monkeypatch):
        async def fake_channels(token, guild_id):
            return [{"id": "chan-1", "name": "general"}], ""
        monkeypatch.setattr("routers.admin.discord_config.get_guild_channels", fake_channels)
        r = await client.get("/admin/api/discord/channels")
        assert r.status_code == 200
        assert r.json() == [{"id": "chan-1", "name": "general"}]

    async def test_guild_reflects_discord_api_error(self, client: AsyncClient, monkeypatch):
        """Same error-path reasoning as the channels/roles tests above —
        this is also the path a not-yet-invited bot hits (403 from
        Discord), which the frontend's config.js treats as non-fatal and
        just leaves the Server column blank rather than failing the
        whole channels list."""
        async def fake_guild(token, guild_id):
            return None, "403 Forbidden — bot missing access to this guild"
        monkeypatch.setattr("routers.admin.discord_config.get_guild_info", fake_guild)
        r = await client.get("/admin/api/discord/guild")
        assert r.status_code == 502

    async def test_guild_success_path(self, client: AsyncClient, monkeypatch):
        async def fake_guild(token, guild_id):
            return {"id": "guild-1", "name": "Kingshot HQ"}, ""
        monkeypatch.setattr("routers.admin.discord_config.get_guild_info", fake_guild)
        r = await client.get("/admin/api/discord/guild")
        assert r.status_code == 200
        assert r.json() == {"id": "guild-1", "name": "Kingshot HQ"}

    async def test_requires_login(self, client_no_session: AsyncClient, tenant: dict):
        r = await client_no_session.get(
            "/admin/api/discord/channels", headers={"X-Tenant-Slug": tenant["slug"]}
        )
        assert r.status_code == 401

    async def test_requires_tenant_access(self, make_user_and_client, tenant: dict):
        client, _ = await make_user_and_client(tenant_grants=[])
        r = await client.get("/admin/api/discord/channels", headers={"X-Tenant-Slug": tenant["slug"]})
        assert r.status_code == 403
        await client.aclose()
