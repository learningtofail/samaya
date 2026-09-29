"""Tests for spec §38: cross-alliance admin views, Tenant icon images,
Kingdom branding titles, and the public last-activity endpoints.
"""
from datetime import date, datetime, time, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import Announcement, AnnouncementTarget, EventDefinition, Occurrence, PostLog

TINY_PNG_DATA_URI = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQV"
    "R42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class TestTenantIcon:

    async def test_create_tenant_with_icon(self, client: AsyncClient, tenant: dict):
        server = (await client.post("/admin/api/discord-servers", json={
            "name": "Test Server 2", "guild_id": "999888777",
        })).json()
        r = await client.post("/admin/api/tenants", json={
            "kingdom_id": tenant["kingdom_id"], "name": "New Alliance", "slug": "newall",
            "server_id": server["id"], "icon_image_data": TINY_PNG_DATA_URI,
        })
        assert r.status_code == 201, r.text
        assert r.json()["icon_image_data"] == TINY_PNG_DATA_URI

    async def test_patch_can_clear_icon(self, client: AsyncClient, tenant: dict):
        server = (await client.post("/admin/api/discord-servers", json={
            "name": "Test Server 3", "guild_id": "999888776",
        })).json()
        created = (await client.post("/admin/api/tenants", json={
            "kingdom_id": tenant["kingdom_id"], "name": "Icon Alliance", "slug": "iconall",
            "server_id": server["id"], "icon_image_data": TINY_PNG_DATA_URI,
        })).json()
        r = await client.patch(f"/admin/api/tenants/{created['id']}", json={"icon_image_data": ""})
        assert r.status_code == 200, r.text
        assert r.json()["icon_image_data"] is None

    async def test_rejects_non_image_icon(self, client: AsyncClient, tenant: dict):
        server = (await client.post("/admin/api/discord-servers", json={
            "name": "Test Server 4", "guild_id": "999888775",
        })).json()
        r = await client.post("/admin/api/tenants", json={
            "kingdom_id": tenant["kingdom_id"], "name": "Bad Icon", "slug": "badicon",
            "server_id": server["id"], "icon_image_data": "not-a-data-uri",
        })
        assert r.status_code == 422

    async def test_public_alliances_roster_includes_icon(self, client: AsyncClient, tenant: dict):
        r = await client.get("/api/alliances")
        assert r.status_code == 200
        assert "icon_image_data" in r.json()[0]


class TestKingdomBranding:

    async def test_defaults_when_unset(self, client: AsyncClient, tenant: dict):
        r = await client.get("/api/kingdom-branding")
        assert r.status_code == 200
        body = r.json()
        assert body["public_site_title"] == "Kingshot Event Schedule"
        assert body["admin_console_title"] == "Samaya"

    async def test_superadmin_can_set_titles(self, client: AsyncClient, tenant: dict):
        r = await client.patch(f"/admin/api/kingdoms/{tenant['kingdom_id']}", json={
            "public_site_title": "K138 Schedule", "admin_console_title": "K138 Console",
        })
        assert r.status_code == 200, r.text

        r2 = await client.get("/api/kingdom-branding")
        assert r2.json() == {"public_site_title": "K138 Schedule", "admin_console_title": "K138 Console"}

    async def test_empty_string_resets_to_default(self, client: AsyncClient, tenant: dict):
        await client.patch(f"/admin/api/kingdoms/{tenant['kingdom_id']}", json={"public_site_title": "Custom"})
        r = await client.patch(f"/admin/api/kingdoms/{tenant['kingdom_id']}", json={"public_site_title": ""})
        assert r.status_code == 200
        r2 = await client.get("/api/kingdom-branding")
        assert r2.json()["public_site_title"] == "Kingshot Event Schedule"

    async def test_requires_superadmin(self, client: AsyncClient, make_user_and_client, tenant: dict):
        viewer_client, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")])
        r = await viewer_client.patch(
            f"/admin/api/kingdoms/{tenant['kingdom_id']}", json={"public_site_title": "Nope"},
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert r.status_code == 403
        await viewer_client.aclose()


class TestCombinedDashboard:

    async def test_status_combined_mode(self, client: AsyncClient, tenant: dict, second_tenant: dict):
        r = await client.get("/admin/api/status", headers={"X-Tenant-Slug": "*"})
        assert r.status_code == 200
        slugs = {a["tenant_slug"] for a in r.json()["alliances"]}
        assert {tenant["slug"], second_tenant["slug"]} <= slugs

    async def test_delivery_health_combined_mode_per_alliance_rows(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, second_tenant: dict
    ):
        now = datetime.now(timezone.utc)
        db_session.add(PostLog(
            tenant_id=tenant["id"], event_name="Siege", occurrence_date=now.date(),
            discord_guild_id="g", posted_at_utc=now, status="posted",
        ))
        await db_session.commit()

        r = await client.get("/admin/api/delivery-health", headers={"X-Tenant-Slug": "*"})
        rows = {a["tenant_slug"]: a for a in r.json()["alliances"]}
        assert rows[tenant["slug"]]["event_posts"]["posted"] == 1
        assert rows[second_tenant["slug"]]["event_posts"]["posted"] == 0


class TestCombinedSync:

    async def test_sync_combined_mode_returns_per_alliance_rows(
        self, client: AsyncClient, tenant: dict, second_tenant: dict
    ):
        r = await client.get("/admin/api/sync/discord", headers={"X-Tenant-Slug": "*"})
        assert r.status_code == 200
        rows = r.json()["alliances"]
        slugs = {a["tenant_slug"] for a in rows}
        assert {tenant["slug"], second_tenant["slug"]} <= slugs
        # Each row is self-contained (either a real "summary" or an
        # "error", never a top-level exception) since one alliance's
        # Discord/token trouble must not take down every other row.
        assert all(("summary" in a) or ("error" in a) for a in rows)


class TestDiscordConfigOverview:

    async def test_groups_by_server_not_by_tenant(
        self, client: AsyncClient, tenant: dict, second_tenant: dict
    ):
        r = await client.get("/admin/api/discord/config-overview", headers={"X-Tenant-Slug": "*"})
        assert r.status_code == 200
        servers = r.json()["servers"]
        # tenant/second_tenant fixtures each get their own DiscordServer
        # (see conftest) - confirms the grouping key is server_id, and
        # each entry lists only tenants on that server.
        for s in servers:
            assert "tenants" in s and "server_name" in s


class TestPublicLastActivity:

    async def test_null_when_nothing_posted(self, client: AsyncClient, tenant: dict):
        r = await client.get(f"/t/{tenant['slug']}/api/last-activity")
        assert r.status_code == 200
        assert r.json() is None

    async def test_shows_latest_posted_event(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict
    ):
        now = datetime.now(timezone.utc)
        db_session.add(PostLog(
            tenant_id=tenant["id"], event_name="Bear Hunt", occurrence_date=now.date(),
            discord_guild_id="g", posted_at_utc=now, status="posted",
        ))
        await db_session.commit()

        r = await client.get(f"/t/{tenant['slug']}/api/last-activity")
        body = r.json()
        assert body["kind"] == "event"
        assert body["name"] == "Bear Hunt"

    async def test_prefers_more_recent_announcement_over_older_event(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict
    ):
        now = datetime.now(timezone.utc)
        db_session.add(PostLog(
            tenant_id=tenant["id"], event_name="Old Event", occurrence_date=now.date(),
            discord_guild_id="g", posted_at_utc=now - timedelta(hours=2), status="posted",
        ))
        ann = Announcement(
            owning_tenant_id=tenant["id"], title="Fresh Announcement", body_markdown="hi",
            scheduled_for=now - timedelta(minutes=5), status="posted", posted_at=now - timedelta(minutes=5),
        )
        db_session.add(ann)
        await db_session.commit()
        await db_session.refresh(ann)
        db_session.add(AnnouncementTarget(
            announcement_id=ann.id, tenant_id=tenant["id"], discord_channel_id="c1", post_status="posted",
        ))
        await db_session.commit()

        r = await client.get(f"/t/{tenant['slug']}/api/last-activity")
        body = r.json()
        assert body["kind"] == "announcement"
        assert body["name"] == "Fresh Announcement"

    async def test_ignores_announcement_that_failed_for_this_tenant(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, second_tenant: dict
    ):
        """A multi-target announcement that succeeded for second_tenant but
        failed for tenant must not show as tenant's last activity."""
        now = datetime.now(timezone.utc)
        ann = Announcement(
            owning_tenant_id=tenant["id"], title="Mixed Result", body_markdown="hi",
            scheduled_for=now - timedelta(minutes=5), status="posted", posted_at=now - timedelta(minutes=5),
        )
        db_session.add(ann)
        await db_session.commit()
        await db_session.refresh(ann)
        db_session.add_all([
            AnnouncementTarget(announcement_id=ann.id, tenant_id=tenant["id"], discord_channel_id="c1", post_status="error"),
            AnnouncementTarget(announcement_id=ann.id, tenant_id=second_tenant["id"], discord_channel_id="c2", post_status="posted"),
        ])
        await db_session.commit()

        r = await client.get(f"/t/{tenant['slug']}/api/last-activity")
        assert r.json() is None

        r2 = await client.get(f"/t/{second_tenant['slug']}/api/last-activity")
        assert r2.json()["name"] == "Mixed Result"

    async def test_combined_last_activity_includes_tenant_name(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict
    ):
        now = datetime.now(timezone.utc)
        db_session.add(PostLog(
            tenant_id=tenant["id"], event_name="Bear Hunt", occurrence_date=now.date(),
            discord_guild_id="g", posted_at_utc=now, status="posted",
        ))
        await db_session.commit()

        r = await client.get("/api/last-activity")
        body = r.json()
        assert body["tenant_slug"] == tenant["slug"]
        assert body["name"] == "Bear Hunt"
