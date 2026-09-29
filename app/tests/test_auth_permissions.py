"""Tests for the permission model itself: who can do what, using
non-superadmin users with specific grants so these tests actually
exercise the checks in routers/admin/deps.py rather than skating past
them the way the superadmin default in `client` would.
"""
import pytest


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

    async def test_tenant_owner_without_kingdom_grant_cannot_create_kingdom_wide_event(
        self, make_user_and_client, tenant: dict
    ):
        """Being an owner of an alliance in the kingdom does NOT imply
        kingdom coordinator access — this is the specific design decision
        from the multi-tenant doc's §3/§7.4."""
        client, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "owner")])
        r = await client.post(
            "/admin/api/events",
            json={
                "name": "Kingdom vs Kingdom", "interval_days": 14, "start_time_utc": "18:00",
                "duration_hours": 3.0, "anchor_date": "2025-05-01", "scope": "kingdom-wide",
            },
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert r.status_code == 403
        await client.aclose()

    async def test_user_with_kingdom_grant_can_create_kingdom_wide_event(
        self, make_user_and_client, tenant: dict
    ):
        client, _ = await make_user_and_client(
            tenant_grants=[(tenant["id"], "owner")], kingdom_grants=[tenant["kingdom_id"]]
        )
        r = await client.post(
            "/admin/api/events",
            json={
                "name": "Kingdom vs Kingdom", "interval_days": 14, "start_time_utc": "18:00",
                "duration_hours": 3.0, "anchor_date": "2025-05-01", "scope": "kingdom-wide",
            },
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert r.status_code == 201, r.text
        await client.aclose()

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


VALID_EVENT = {
    "name":            "Viking Vengeance",
    "interval_days":   14,
    "start_time_utc":  "20:00",
    "duration_hours":  1.0,
    "discord_channel": "#alliance-war",
    "description":     "Biweekly alliance PvP",
    "anchor_date":     "2025-05-04",
}


class TestViewerRole:
    """spec §31 — the read-only Viewer role. Covers every router where
    Depends(get_current_tenant) was swapped for Depends(require_not_viewer):
    events, announcements, announcement_templates, occurrences,
    discord_sync, scheduler_control. A viewer passes every GET the same as
    a coordinator would (get_current_tenant's plain access check still
    applies), but any POST/PATCH/DELETE/PUT on tenant data 403s."""

    async def test_viewer_can_list_events(self, make_user_and_client, tenant: dict):
        client, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")])
        r = await client.get("/admin/api/events", headers={"X-Tenant-Slug": tenant["slug"]})
        assert r.status_code == 200
        await client.aclose()

    async def test_viewer_cannot_create_event(self, make_user_and_client, tenant: dict):
        client, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")])
        r = await client.post(
            "/admin/api/events", json=VALID_EVENT, headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert r.status_code == 403
        assert "read-only" in r.json()["detail"].lower()
        await client.aclose()

    async def test_viewer_cannot_patch_event(self, make_user_and_client, client, tenant: dict):
        created = await client.post("/admin/api/events", json=VALID_EVENT)
        event_id = created.json()["id"]
        viewer, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")])
        r = await viewer.patch(
            f"/admin/api/events/{event_id}", json={"name": "New Name"},
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert r.status_code == 403
        await viewer.aclose()

    async def test_viewer_cannot_delete_event(self, make_user_and_client, client, tenant: dict):
        created = await client.post("/admin/api/events", json=VALID_EVENT)
        event_id = created.json()["id"]
        viewer, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")])
        r = await viewer.delete(
            f"/admin/api/events/{event_id}/permanent", headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert r.status_code == 403
        await viewer.aclose()

    async def test_viewer_can_list_announcements(self, make_user_and_client, tenant: dict):
        client, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")])
        r = await client.get("/admin/api/announcements", headers={"X-Tenant-Slug": tenant["slug"]})
        assert r.status_code == 200
        await client.aclose()

    async def test_viewer_cannot_create_announcement(self, make_user_and_client, tenant: dict):
        client, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")])
        r = await client.post(
            "/admin/api/announcements",
            json={
                "title": "Reminder", "body_markdown": "hi",
                "scheduled_for": "2099-01-01T00:00:00Z",
                "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
            },
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert r.status_code == 403
        await client.aclose()

    async def test_viewer_cannot_create_announcement_template(self, make_user_and_client, tenant: dict):
        client, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")])
        r = await client.post(
            "/admin/api/announcement-templates",
            json={"name": "Reset Warning", "title_template": "t", "body_template": "b"},
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert r.status_code == 403
        await client.aclose()

    async def test_viewer_cannot_patch_occurrence(self, make_user_and_client, client, tenant: dict):
        await client.post("/admin/api/events", json=VALID_EVENT)
        await client.post("/admin/api/scheduler/regenerate")
        occs = (await client.get("/admin/api/occurrences")).json()
        occ_id = occs[0]["id"]
        viewer, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")])
        r = await viewer.patch(
            f"/admin/api/occurrences/{occ_id}", json={"post_to_discord": False},
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert r.status_code == 403
        await viewer.aclose()

    async def test_viewer_cannot_post_occurrence(self, make_user_and_client, client, tenant: dict):
        await client.post("/admin/api/events", json=VALID_EVENT)
        await client.post("/admin/api/scheduler/regenerate")
        occs = (await client.get("/admin/api/occurrences")).json()
        occ_id = occs[0]["id"]
        viewer, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")])
        r = await viewer.post(
            f"/admin/api/occurrences/{occ_id}/post", headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert r.status_code == 403
        await viewer.aclose()

    async def test_viewer_cannot_trigger_discord_sync(self, make_user_and_client, tenant: dict):
        # A nonexistent post_log_id is fine here — require_not_viewer is a
        # FastAPI dependency, resolved (and its 403 raised) before the
        # route body ever looks the row up, so this proves the permission
        # check runs first rather than proving anything about the lookup.
        client, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")])
        r = await client.post(
            "/admin/api/sync/mark-cancelled/999999", headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert r.status_code == 403
        await client.aclose()

    async def test_viewer_cannot_trigger_manual_regenerate(self, make_user_and_client, tenant: dict):
        client, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")])
        r = await client.post(
            "/admin/api/scheduler/regenerate", headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert r.status_code == 403
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
