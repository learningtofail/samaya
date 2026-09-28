"""Tests for the access-management surface added on top of invites.py's
existing invite lifecycle: editing/removing an already-claimed UserTenant
or UserKingdom grant (routers/admin/invites.py's /api/members and
/api/kingdom-coordinators), Kingdom editing (routers/admin/tenants.py's
PATCH /api/kingdoms/{id}), and platform-wide user/superadmin management
(routers/admin/users.py).
"""
from httpx import AsyncClient


class TestTenantMembers:

    async def test_list_members(self, client: AsyncClient, tenant: dict, make_user_and_client):
        other, other_id = await make_user_and_client(tenant_grants=[(tenant["id"], "coordinator")])
        r = await client.get("/admin/api/members", headers={"X-Tenant-Slug": tenant["slug"]})
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 1
        assert body[0]["user_id"] == other_id
        assert body[0]["role"] == "coordinator"
        await other.aclose()

    async def test_change_role(self, client: AsyncClient, tenant: dict, make_user_and_client):
        other, other_id = await make_user_and_client(tenant_grants=[(tenant["id"], "coordinator")])
        members = (await client.get("/admin/api/members", headers={"X-Tenant-Slug": tenant["slug"]})).json()
        member_id = members[0]["id"]

        r = await client.patch(
            f"/admin/api/members/{member_id}", json={"role": "owner"},
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert r.status_code == 200
        assert r.json()["role"] == "owner"
        await other.aclose()

    async def test_cannot_demote_sole_owner(self, client: AsyncClient, tenant: dict, make_user_and_client):
        other, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "owner")])
        members = (await client.get("/admin/api/members", headers={"X-Tenant-Slug": tenant["slug"]})).json()
        member_id = members[0]["id"]

        r = await client.patch(
            f"/admin/api/members/{member_id}", json={"role": "coordinator"},
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert r.status_code == 400
        await other.aclose()

    async def test_cannot_remove_sole_owner(self, client: AsyncClient, tenant: dict, make_user_and_client):
        other, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "owner")])
        members = (await client.get("/admin/api/members", headers={"X-Tenant-Slug": tenant["slug"]})).json()
        member_id = members[0]["id"]

        r = await client.delete(f"/admin/api/members/{member_id}", headers={"X-Tenant-Slug": tenant["slug"]})
        assert r.status_code == 400
        await other.aclose()

    async def test_remove_member(self, client: AsyncClient, tenant: dict, make_user_and_client):
        other, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "coordinator")])
        members = (await client.get("/admin/api/members", headers={"X-Tenant-Slug": tenant["slug"]})).json()
        member_id = members[0]["id"]

        r = await client.delete(f"/admin/api/members/{member_id}", headers={"X-Tenant-Slug": tenant["slug"]})
        assert r.status_code == 200

        r2 = await client.get("/admin/api/members", headers={"X-Tenant-Slug": tenant["slug"]})
        assert r2.json() == []
        await other.aclose()

    async def test_coordinator_cannot_manage_members(self, tenant: dict, make_user_and_client):
        coordinator, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "coordinator")])
        r = await coordinator.get("/admin/api/members", headers={"X-Tenant-Slug": tenant["slug"]})
        assert r.status_code == 403
        await coordinator.aclose()


class TestKingdomCoordinators:

    async def test_list_and_remove(self, client: AsyncClient, tenant: dict, make_user_and_client):
        other, other_id = await make_user_and_client(kingdom_grants=[tenant["kingdom_id"]])
        r = await client.get(
            "/admin/api/kingdom-coordinators", params={"kingdom_id": tenant["kingdom_id"]},
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 1
        assert body[0]["user_id"] == other_id

        r2 = await client.delete(
            f"/admin/api/kingdom-coordinators/{body[0]['id']}", headers={"X-Tenant-Slug": tenant["slug"]}
        )
        assert r2.status_code == 200
        await other.aclose()

    async def test_requires_superadmin(self, tenant: dict, make_user_and_client):
        owner, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "owner")])
        r = await owner.get(
            "/admin/api/kingdom-coordinators", params={"kingdom_id": tenant["kingdom_id"]},
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert r.status_code == 403
        await owner.aclose()


class TestKingdomPatch:

    async def test_update_kingdom(self, client: AsyncClient, tenant: dict):
        r = await client.patch(
            f"/admin/api/kingdoms/{tenant['kingdom_id']}", json={"name": "Kingdom 138 (Renamed)"},
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert r.status_code == 200
        assert r.json()["name"] == "Kingdom 138 (Renamed)"

    async def test_404_for_missing_kingdom(self, client: AsyncClient, tenant: dict):
        r = await client.patch(
            "/admin/api/kingdoms/999999", json={"name": "Nope"}, headers={"X-Tenant-Slug": tenant["slug"]}
        )
        assert r.status_code == 404

    async def test_requires_superadmin(self, tenant: dict, make_user_and_client):
        owner, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "owner")])
        r = await owner.patch(
            f"/admin/api/kingdoms/{tenant['kingdom_id']}", json={"name": "Nope"},
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert r.status_code == 403
        await owner.aclose()


class TestUserManagement:

    async def test_list_users(self, client: AsyncClient, tenant: dict, test_user: dict):
        r = await client.get("/admin/api/users", headers={"X-Tenant-Slug": tenant["slug"]})
        assert r.status_code == 200
        assert any(u["id"] == test_user["id"] for u in r.json())

    async def test_grant_and_revoke_superadmin(self, client: AsyncClient, tenant: dict, make_user_and_client):
        other, other_id = await make_user_and_client(tenant_grants=[])
        r = await client.patch(
            f"/admin/api/users/{other_id}", json={"is_superadmin": True},
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert r.status_code == 200
        assert r.json()["is_superadmin"] is True

        r2 = await client.patch(
            f"/admin/api/users/{other_id}", json={"is_superadmin": False},
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert r2.status_code == 200
        assert r2.json()["is_superadmin"] is False
        await other.aclose()

    async def test_cannot_revoke_own_superadmin(self, client: AsyncClient, tenant: dict, test_user: dict):
        r = await client.patch(
            f"/admin/api/users/{test_user['id']}", json={"is_superadmin": False},
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert r.status_code == 400

    async def test_requires_superadmin(self, tenant: dict, make_user_and_client):
        owner, owner_id = await make_user_and_client(tenant_grants=[(tenant["id"], "owner")])
        r = await owner.get("/admin/api/users", headers={"X-Tenant-Slug": tenant["slug"]})
        assert r.status_code == 403
        await owner.aclose()
