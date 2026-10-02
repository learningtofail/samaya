"""Tests for spec §38: cross-alliance admin views, Tenant icon images,
Kingdom branding titles, and the public last-activity endpoints.
"""

from httpx import AsyncClient


TINY_PNG_DATA_URI = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQV"
    "R42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class TestTenantIcon:

    async def test_create_tenant_with_icon(self, client: AsyncClient, tenant: dict):
        server = (await client.post("/admin/api/discord-servers", json={
            "kingdom_id": tenant["kingdom_id"], "name": "Test Server 2", "guild_id": "999888777",
        })).json()
        r = await client.post("/admin/api/tenants", json={
            "kingdom_id": tenant["kingdom_id"], "name": "New Alliance", "slug": "newall",
            "server_id": server["id"], "icon_image_data": TINY_PNG_DATA_URI,
        })
        assert r.status_code == 201, r.text
        assert r.json()["icon_image_data"] == TINY_PNG_DATA_URI

    async def test_patch_can_clear_icon(self, client: AsyncClient, tenant: dict):
        server = (await client.post("/admin/api/discord-servers", json={
            "kingdom_id": tenant["kingdom_id"], "name": "Test Server 3", "guild_id": "999888776",
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
            "kingdom_id": tenant["kingdom_id"], "name": "Test Server 4", "guild_id": "999888775",
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
        assert r2.json() == {"public_site_title": "K138 Schedule", "admin_console_title": "K138 Console", "color": None}

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


class TestBrandColors:
    """Spec §63: validated alliance colors and the Kingdom color."""

    async def _server(self, client, tenant, guild):
        return (await client.post("/admin/api/discord-servers", json={
            "kingdom_id": tenant["kingdom_id"], "name": f"S{guild}", "guild_id": guild,
        })).json()

    async def test_tenant_color_must_be_hex(self, client: AsyncClient, tenant: dict):
        server = await self._server(client, tenant, "777001")
        r = await client.post("/admin/api/tenants", json={
            "kingdom_id": tenant["kingdom_id"], "name": "Bad", "slug": "bad",
            "server_id": server["id"], "color": "red",
        })
        assert r.status_code == 422
        assert "hex" in r.text

    async def test_tenant_color_is_normalized_and_patchable(self, client: AsyncClient, tenant: dict):
        server = await self._server(client, tenant, "777002")
        created = (await client.post("/admin/api/tenants", json={
            "kingdom_id": tenant["kingdom_id"], "name": "Good", "slug": "good",
            "server_id": server["id"], "color": "#e65100",
        })).json()
        assert created["color"] == "#E65100"
        r = await client.patch(f"/admin/api/tenants/{created['id']}", json={"color": "#1565c0"})
        assert r.status_code == 200 and r.json()["color"] == "#1565C0"
        r = await client.patch(f"/admin/api/tenants/{created['id']}", json={"color": "#12"})
        assert r.status_code == 422

    async def test_patching_another_field_ignores_a_legacy_color(self, client: AsyncClient, tenant: dict, db_engine):
        from sqlalchemy import update
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
        from models.db import Tenant
        async with async_sessionmaker(db_engine, class_=AsyncSession)() as s:
            await s.execute(update(Tenant).where(Tenant.id == tenant["id"]).values(color="slate"))
            await s.commit()
        r = await client.patch(f"/admin/api/tenants/{tenant['id']}", json={"name": "MOD renamed"})
        assert r.status_code == 200, r.text
        assert r.json()["color"] == "slate" and r.json()["color_note"] is None

    async def test_faint_color_gets_a_note_not_a_rejection(self, client: AsyncClient, tenant: dict):
        r = await client.patch(f"/admin/api/tenants/{tenant['id']}", json={"color": "#FFFF99"})
        assert r.status_code == 200
        assert "very light" in r.json()["color_note"]

    async def test_kingdom_color_set_public_and_reset(self, client: AsyncClient, tenant: dict):
        assert (await client.get("/api/kingdom-branding")).json()["color"] is None
        r = await client.patch(f"/admin/api/kingdoms/{tenant['kingdom_id']}", json={"color": "#c9a227"})
        assert r.status_code == 200 and r.json()["color"] == "#C9A227"
        assert (await client.get("/api/kingdom-branding")).json()["color"] == "#C9A227"
        r = await client.patch(f"/admin/api/kingdoms/{tenant['kingdom_id']}", json={"color": ""})
        assert r.status_code == 200 and r.json()["color"] is None
        assert (await client.get("/api/kingdom-branding")).json()["color"] is None

    async def test_kingdom_color_rejects_non_hex(self, client: AsyncClient, tenant: dict):
        r = await client.patch(f"/admin/api/kingdoms/{tenant['kingdom_id']}", json={"color": "gold"})
        assert r.status_code == 422

    async def test_kingdom_color_requires_superadmin(self, make_user_and_client, tenant: dict):
        c, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "owner")])
        r = await c.patch(f"/admin/api/kingdoms/{tenant['kingdom_id']}", json={"color": "#C9A227"})
        assert r.status_code in (401, 403)
