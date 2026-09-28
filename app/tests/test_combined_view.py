"""Combined-mode ('X-Tenant-Slug: *') coverage for the admin list
endpoints touched by spec §14 — events, occurrences, post-log. Write
endpoints are untouched by that section and stay covered by their own
existing test files (test_routes_events.py, test_post_occurrence.py).
"""
import pytest


@pytest.mark.anyio
async def test_events_combined_includes_both_tenants(client, tenant, second_tenant):
    client.headers["X-Tenant-Slug"] = tenant["slug"]
    await client.post("/admin/api/events", json={
        "name": "MOD Only Event", "scope": "alliance", "interval_days": 7,
        "start_time_utc": "19:00", "duration_hours": 2, "anchor_date": "2026-01-01",
        "discord_channel": "general", "description": "",
    })
    client.headers["X-Tenant-Slug"] = second_tenant["slug"]
    await client.post("/admin/api/events", json={
        "name": "NSR Only Event", "scope": "alliance", "interval_days": 7,
        "start_time_utc": "20:00", "duration_hours": 2, "anchor_date": "2026-01-01",
        "discord_channel": "general", "description": "",
    })

    client.headers["X-Tenant-Slug"] = "*"
    resp = await client.get("/admin/api/events")
    assert resp.status_code == 200
    names = {e["name"] for e in resp.json()}
    assert names == {"MOD Only Event", "NSR Only Event"}


@pytest.mark.anyio
async def test_events_combined_dedupes_kingdom_wide(client, tenant, second_tenant):
    """A kingdom-wide event owned by one tenant is visible to the other
    (read-visibility, per admin/events.py's own docstring) — combined
    mode must not show it twice just because both tenants are unioned in.
    """
    client.headers["X-Tenant-Slug"] = tenant["slug"]
    await client.post("/admin/api/events", json={
        "name": "Kingdom Clash", "scope": "kingdom-wide", "interval_days": 7,
        "start_time_utc": "19:00", "duration_hours": 2, "anchor_date": "2026-01-01",
        "discord_channel": "general", "description": "",
    })

    client.headers["X-Tenant-Slug"] = "*"
    resp = await client.get("/admin/api/events")
    assert resp.status_code == 200
    matches = [e for e in resp.json() if e["name"] == "Kingdom Clash"]
    assert len(matches) == 1


@pytest.mark.anyio
async def test_combined_mode_respects_access_for_non_superadmin(
    client, tenant, second_tenant, make_user_and_client
):
    """A coordinator with access to only `tenant` must not see
    `second_tenant`'s events through combined mode — '*' means every
    tenant *this user* can reach, not every tenant that exists.
    """
    client.headers["X-Tenant-Slug"] = second_tenant["slug"]
    await client.post("/admin/api/events", json={
        "name": "NSR Private Event", "scope": "alliance", "interval_days": 7,
        "start_time_utc": "20:00", "duration_hours": 2, "anchor_date": "2026-01-01",
        "discord_channel": "general", "description": "",
    })

    limited_client, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "owner")])
    limited_client.headers["X-Tenant-Slug"] = "*"
    resp = await limited_client.get("/admin/api/events")
    assert resp.status_code == 200
    names = {e["name"] for e in resp.json()}
    assert "NSR Private Event" not in names
    await limited_client.aclose()


@pytest.mark.anyio
async def test_occurrences_and_post_log_combined_union_tenants(
    client, tenant, second_tenant, db_engine
):
    """Lighter-weight check than the events test above: combined mode on
    /api/occurrences and /api/post-log doesn't error and scopes to the
    caller's own accessible tenants (both, here) rather than raising on
    the '*' sentinel the way a plain tenant-slug lookup would.
    """
    client.headers["X-Tenant-Slug"] = "*"
    occ_resp = await client.get("/admin/api/occurrences")
    assert occ_resp.status_code == 200

    log_resp = await client.get("/admin/api/post-log")
    assert log_resp.status_code == 200

    csv_resp = await client.get("/admin/api/post-log/export.csv")
    assert csv_resp.status_code == 200


@pytest.mark.anyio
async def test_combined_mode_write_endpoints_unaffected(client, tenant):
    """§14.3: '*' is a read/list convenience only. Creating an event still
    requires one real tenant in X-Tenant-Slug — write endpoints depend on
    get_current_tenant directly, which has no notion of '*'.
    """
    client.headers["X-Tenant-Slug"] = "*"
    resp = await client.post("/admin/api/events", json={
        "name": "Should Fail", "scope": "alliance", "interval_days": 7,
        "start_time_utc": "19:00", "duration_hours": 2, "anchor_date": "2026-01-01",
        "discord_channel": "general", "description": "",
    })
    assert resp.status_code == 404
