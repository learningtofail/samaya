"""Tests for the permission model itself: who can do what, using
non-superadmin users with specific grants so these tests actually
exercise the checks in routers/admin/deps.py rather than skating past
them the way the superadmin default in `client` would.
"""


class TestNoSession:

    async def test_no_cookie_is_401(self, client_no_session):
        r = await client_no_session.get("/admin/api/events", headers={"X-Tenant-Slug": "mod"})
        assert r.status_code == 401


class TestNoTenantAccess:

    async def test_user_with_no_grant_for_tenant_is_403(
        self, make_user_and_client, tenant: dict
    ):
        client, _ = await make_user_and_client(tenant_grants=[])
        r = await client.get("/admin/api/events", headers={"X-Tenant-Slug": tenant["slug"]})
        assert r.status_code == 403
        await client.aclose()

    async def test_unknown_tenant_slug_is_404_even_for_superadmin(self, client):
        r = await client.get("/admin/api/events", headers={"X-Tenant-Slug": "nonexistent"})
        assert r.status_code == 404


class TestCoordinatorVsOwner:

    async def test_coordinator_can_list_and_create_events(
        self, make_user_and_client, tenant: dict
    ):
        client, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "coordinator")])
        r = await client.get("/admin/api/events", headers={"X-Tenant-Slug": tenant["slug"]})
        assert r.status_code == 200
        await client.aclose()

    async def test_coordinator_cannot_create_invites(
        self, make_user_and_client, tenant: dict
    ):
        client, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "coordinator")])
        r = await client.post(
            "/admin/api/invites", json={"role": "coordinator"},
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert r.status_code == 403
        await client.aclose()

    async def test_owner_can_create_invites(self, make_user_and_client, tenant: dict):
        client, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "owner")])
        r = await client.post(
            "/admin/api/invites", json={"role": "coordinator"},
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert r.status_code == 201, r.text
        assert r.json()["role"] == "coordinator"
        await client.aclose()

    async def test_owner_can_revoke_own_tenants_invite(self, make_user_and_client, tenant: dict):
        client, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "owner")])
        headers = {"X-Tenant-Slug": tenant["slug"]}
        created = await client.post("/admin/api/invites", json={"role": "coordinator"}, headers=headers)
        invite_id = created.json()["id"]

        r = await client.delete(f"/admin/api/invites/{invite_id}", headers=headers)
        assert r.status_code == 200
        await client.aclose()


class TestKingdomCoordinator:



    async def test_kingdom_coordinator_invite_is_superadmin_only(
        self, make_user_and_client, tenant: dict
    ):
        client, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "owner")])
        r = await client.post("/admin/api/kingdom-invites", json={"kingdom_id": tenant["kingdom_id"]})
        assert r.status_code == 403
        await client.aclose()

    async def test_superadmin_can_create_kingdom_coordinator_invite(self, client, tenant: dict):
        r = await client.post("/admin/api/kingdom-invites", json={"kingdom_id": tenant["kingdom_id"]})
        assert r.status_code == 201, r.text
        assert r.json()["role"] == "kingdom_coordinator"


class TestTenantCrudIsSuperadminOnly:

    async def test_non_superadmin_cannot_create_tenant(
        self, make_user_and_client, tenant: dict
    ):
        client, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "owner")])
        r = await client.post(
            "/admin/api/tenants",
            json={"kingdom_id": tenant["kingdom_id"], "name": "New Alliance", "slug": "newalliance", "server_id": tenant["server_id"]},
        )
        assert r.status_code == 403
        await client.aclose()

    async def test_superadmin_can_create_tenant(self, client, tenant: dict):
        r = await client.post(
            "/admin/api/tenants",
            json={"kingdom_id": tenant["kingdom_id"], "name": "New Alliance", "slug": "newalliance", "server_id": tenant["server_id"]},
        )
        assert r.status_code == 201, r.text

    async def test_list_tenants_scoped_to_own_access_for_non_superadmin(
        self, make_user_and_client, tenant: dict, second_tenant: dict
    ):
        client, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "owner")])
        r = await client.get("/admin/api/tenants")
        slugs = [t["slug"] for t in r.json()]
        assert tenant["slug"] in slugs
        assert second_tenant["slug"] not in slugs
        await client.aclose()

    async def test_list_tenants_shows_everything_for_superadmin(
        self, client, tenant: dict, second_tenant: dict
    ):
        r = await client.get("/admin/api/tenants")
        slugs = [t["slug"] for t in r.json()]
        assert tenant["slug"] in slugs
        assert second_tenant["slug"] in slugs


class TestViewerRole:
    """spec §31 — the read-only Viewer role. A viewer passes every GET the
    same as a coordinator would, but any POST/PATCH/DELETE/PUT on tenant
    data 403s. Event, event type and delivery writes are covered in
    test_unified_events.py and test_event_engine.py; this class covers the
    role-management side."""

    async def test_viewer_can_list_events(self, make_user_and_client, tenant: dict):
        client, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")])
        r = await client.get("/admin/api/events", headers={"X-Tenant-Slug": tenant["slug"]})
        assert r.status_code == 200
        await client.aclose()











    async def test_owner_can_change_a_coordinators_role_to_viewer(
        self, make_user_and_client, tenant: dict
    ):
        owner, _ = await make_user_and_client(
            tenant_grants=[(tenant["id"], "owner")], discord_id="owner-1",
        )
        coordinator, coord_user_id = await make_user_and_client(
            tenant_grants=[(tenant["id"], "coordinator")], discord_id="coordinator-1",
        )
        members = (await owner.get(
            "/admin/api/members", headers={"X-Tenant-Slug": tenant["slug"]},
        )).json()
        member_id = next(m["id"] for m in members if m["discord_id"] == "coordinator-1")
        r = await owner.patch(
            f"/admin/api/members/{member_id}", json={"role": "viewer"},
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["role"] == "viewer"
        await owner.aclose()
        await coordinator.aclose()
