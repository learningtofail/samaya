"""Tests for routers/admin/status.py and routers/admin/discord_config.py.
Both had zero test coverage before this file — confirmed not currently
broken via a manual smoke test during the Phase 4/5 code-quality pass,
but "I checked once by hand" isn't the same as having a real test, so
these lock in what that smoke test checked.
"""
from httpx import AsyncClient


class TestStatus:

    async def test_returns_ok_shape(self, client: AsyncClient, tenant: dict):
        r = await client.get("/admin/api/status")
        assert r.status_code == 200
        body = r.json()
        assert body["service"] == "samaya"
        assert body["tenant"] == tenant["slug"]
        assert "scheduler" in body
        assert "regenerate_occurrences" in body["scheduler"]
        assert "pre_event_notifier" in body["scheduler"]

    async def test_requires_login(self, client_no_session: AsyncClient, tenant: dict):
        r = await client_no_session.get("/admin/api/status", headers={"X-Tenant-Slug": tenant["slug"]})
        assert r.status_code == 401

    async def test_scoped_to_current_tenant(
        self, client: AsyncClient, tenant: dict, second_tenant: dict
    ):
        r1 = await client.get("/admin/api/status", headers={"X-Tenant-Slug": tenant["slug"]})
        r2 = await client.get("/admin/api/status", headers={"X-Tenant-Slug": second_tenant["slug"]})
        assert r1.json()["tenant"] == tenant["slug"]
        assert r2.json()["tenant"] == second_tenant["slug"]


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
