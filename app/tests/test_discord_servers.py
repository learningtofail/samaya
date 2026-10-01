"""Tests for spec §25 — DiscordServer as a first-class entity. Covers the
new /admin/api/discord-servers CRUD surface, superadmin gating, Tenant
create/update now going through server_id, and the sharing behavior that
motivated the whole redesign: two Tenants on the same DiscordServer share
its bot_token/guild_id, with no per-tenant override.
"""
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import DiscordServer, Tenant


@pytest.fixture
def fake_verified_bot_token(monkeypatch):
    """verify_token calls Discord's real /users/@me — every test that sets
    a bot_token needs this stubbed, same as no test exercises the real
    Discord OAuth round-trip (see conftest.py's own note on that)."""
    async def _fake_verify(token):
        return True, "fake-bot#0000"
    monkeypatch.setattr("routers.admin.tenants.verify_token", _fake_verify)


class TestDiscordServerCrud:

    async def test_list_discord_servers(self, client: AsyncClient, tenant: dict):
        r = await client.get("/admin/api/discord-servers")
        assert r.status_code == 200
        body = r.json()
        assert any(s["guild_id"] == "test-guild-mod" for s in body)

    async def test_list_includes_tenant_names(self, client: AsyncClient, tenant: dict):
        r = await client.get("/admin/api/discord-servers")
        server = next(s for s in r.json() if s["guild_id"] == "test-guild-mod")
        assert server["tenant_names"] == ["MOD"]

    async def test_create_discord_server(self, client: AsyncClient, tenant):
        r = await client.post("/admin/api/discord-servers", json={"kingdom_id": tenant["kingdom_id"], "name": "HTD", "guild_id": "g-htd"})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["name"] == "HTD"
        assert body["guild_id"] == "g-htd"
        assert body["has_own_bot_token"] is False
        assert body["tenant_names"] == []

    async def test_create_discord_server_never_returns_bot_token(self, client: AsyncClient, tenant, fake_verified_bot_token):
        r = await client.post(
            "/admin/api/discord-servers", json={"kingdom_id": tenant["kingdom_id"], "name": "HTD", "guild_id": "g-htd2", "bot_token": "secret-token"}
        )
        assert r.status_code == 201, r.text
        assert "bot_token" not in r.json()
        assert r.json()["has_own_bot_token"] is True

    async def test_update_discord_server_name_and_guild_id(self, client: AsyncClient, tenant: dict):
        servers = (await client.get("/admin/api/discord-servers")).json()
        server_id = next(s["id"] for s in servers if s["guild_id"] == "test-guild-mod")

        r = await client.patch(f"/admin/api/discord-servers/{server_id}", json={"name": "MOD's Discord"})
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "MOD's Discord"
        # Sharing not disturbed — still just the one tenant on it.
        assert r.json()["tenant_names"] == ["MOD"]

    async def test_update_nonexistent_server_404s(self, client: AsyncClient):
        r = await client.patch("/admin/api/discord-servers/999999", json={"name": "Nope"})
        assert r.status_code == 404


class TestDiscordServerSuperadminOnly:

    async def test_non_superadmin_cannot_list_discord_servers(
        self, make_user_and_client, tenant: dict
    ):
        client, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "owner")])
        r = await client.get("/admin/api/discord-servers")
        assert r.status_code == 403
        await client.aclose()

    async def test_non_superadmin_cannot_create_discord_server(
        self, make_user_and_client, tenant: dict
    ):
        client, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "owner")])
        r = await client.post("/admin/api/discord-servers", json={"kingdom_id": tenant["kingdom_id"], "name": "HTD", "guild_id": "g-htd"})
        assert r.status_code == 403
        await client.aclose()


class TestTenantServerAssignment:

    async def test_create_tenant_with_server_id(self, client: AsyncClient, tenant: dict):
        r = await client.post(
            "/admin/api/tenants",
            json={"kingdom_id": tenant["kingdom_id"], "name": "New Alliance", "slug": "newalliance", "server_id": tenant["server_id"]},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["server_id"] == tenant["server_id"]
        assert body["guild_id"] == "test-guild-mod"

    async def test_create_tenant_with_unknown_server_404s(self, client: AsyncClient, tenant: dict):
        r = await client.post(
            "/admin/api/tenants",
            json={"kingdom_id": tenant["kingdom_id"], "name": "New Alliance", "slug": "newalliance", "server_id": 999999},
        )
        assert r.status_code == 404

    async def test_update_tenant_server_id(self, client: AsyncClient, tenant: dict):
        r = await client.post("/admin/api/discord-servers", json={"kingdom_id": tenant["kingdom_id"], "name": "HTD", "guild_id": "g-htd-move"})
        new_server_id = r.json()["id"]

        r = await client.patch(f"/admin/api/tenants/{tenant['id']}", json={"server_id": new_server_id})
        assert r.status_code == 200, r.text
        assert r.json()["server_id"] == new_server_id
        assert r.json()["guild_id"] == "g-htd-move"

    async def test_update_tenant_with_unknown_server_404s(self, client: AsyncClient, tenant: dict):
        r = await client.patch(f"/admin/api/tenants/{tenant['id']}", json={"server_id": 999999})
        assert r.status_code == 404

    async def test_tenant_dict_no_longer_exposes_bot_token_fields(self, client: AsyncClient, tenant: dict):
        r = await client.get("/admin/api/tenants")
        body = r.json()[0]
        assert "bot_token" not in body
        assert "public_key" not in body
        assert "has_own_bot_token" not in body


class TestServerSharingBehavior:
    """The whole point of spec §25: two alliances can now explicitly share
    one Discord server, and sharing it means sharing its single bot."""

    async def test_two_tenants_can_share_one_server(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict
    ):
        server = await db_session.get(DiscordServer, tenant["server_id"])
        other = Tenant(kingdom_id=tenant["kingdom_id"], server_id=server.id, name="Shares MOD's server", slug="sharer")
        db_session.add(other)
        await db_session.commit()

        r = await client.get("/admin/api/discord-servers")
        row = next(s for s in r.json() if s["id"] == tenant["server_id"])
        assert sorted(row["tenant_names"]) == ["MOD", "Shares MOD's server"]

    async def test_updating_shared_server_bot_token_affects_every_tenant_on_it(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, fake_verified_bot_token
    ):
        other = Tenant(kingdom_id=tenant["kingdom_id"], server_id=tenant["server_id"], name="Sharer", slug="sharer2")
        db_session.add(other)
        await db_session.commit()

        r = await client.patch(f"/admin/api/discord-servers/{tenant['server_id']}", json={"bot_token": "new-shared-token"})
        assert r.status_code == 200, r.text

        server = await db_session.get(DiscordServer, tenant["server_id"])
        await db_session.refresh(server)
        assert server.bot_token == "new-shared-token"
        # No per-tenant override exists to check against — this IS the
        # source of truth both tenants now resolve through (deps.py's
        # get_discord_config reads tenant.server.bot_token directly).


class TestDuplicateConstraintMessages:
    """Audit remediation, Phase 2 — services/db_errors.py replaces a mix of
    fragile str(exception) substring matching and raw-exception-text
    leakage with one shared IntegrityError -> friendly-422 translator.
    These pin the actual message text a client sees (not just the status
    code), and specifically exercise the UNIQUE path — SQLite (this test
    suite's own driver) reports a unique violation completely differently
    from asyncpg (production's driver), so this is what proves the two
    drivers' error text both resolve to the same friendly message rather
    than only working in production and silently degrading to the generic
    fallback here."""

    async def test_duplicate_tenant_slug_on_create(self, client: AsyncClient, tenant: dict):
        r = await client.post(
            "/admin/api/tenants",
            json={"kingdom_id": tenant["kingdom_id"], "name": "Copycat", "slug": tenant["slug"], "server_id": tenant["server_id"]},
        )
        assert r.status_code == 422
        assert r.json()["detail"] == "A tenant with that slug already exists"

    async def test_duplicate_tenant_slug_on_update(self, client: AsyncClient, tenant: dict, second_tenant: dict):
        r = await client.patch(f"/admin/api/tenants/{second_tenant['id']}", json={"slug": tenant["slug"]})
        assert r.status_code == 422
        assert r.json()["detail"] == "A tenant with that slug already exists"

    async def test_duplicate_discord_server_guild_id_on_create(self, client: AsyncClient, tenant: dict):
        r = await client.post("/admin/api/discord-servers", json={"kingdom_id": tenant["kingdom_id"], "name": "Copycat", "guild_id": "test-guild-mod"})
        assert r.status_code == 422
        assert r.json()["detail"] == "A Discord server with that guild ID already exists"

    async def test_duplicate_discord_server_guild_id_on_update(
        self, client: AsyncClient, tenant: dict, second_tenant: dict
    ):
        r = await client.patch(f"/admin/api/discord-servers/{tenant['server_id']}", json={"guild_id": "test-guild-nsr"})
        assert r.status_code == 422
        assert r.json()["detail"] == "A Discord server with that guild ID already exists"

    async def test_duplicate_kingdom_slug_on_create(self, client: AsyncClient, tenant: dict):
        r = await client.post("/admin/api/kingdoms", json={"name": "Copycat Kingdom", "slug": "k138"})
        assert r.status_code == 422
        assert r.json()["detail"] == "A kingdom with that slug already exists"

    async def test_duplicate_kingdom_slug_on_update(self, client: AsyncClient, tenant: dict):
        r = await client.post("/admin/api/kingdoms", json={"name": "Second Kingdom", "slug": "k999"})
        assert r.status_code == 201, r.text
        second_kingdom_id = r.json()["id"]

        r = await client.patch(f"/admin/api/kingdoms/{second_kingdom_id}", json={"slug": "k138"})
        assert r.status_code == 422
        assert r.json()["detail"] == "A kingdom with that slug already exists"
