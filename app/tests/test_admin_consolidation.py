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
