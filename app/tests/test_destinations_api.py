"""Spec §67: servers with a Kingdom, secondary servers, destinations, audience
groups, event selection, preview and the grouped delivery log."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from models.db import Delivery, Kingdom
from services.event_engine import run_delivery_tick
from tests.unified_helpers import deliveries

A = "/admin/api"


def _hdr(client, slug):
    client.headers["X-Tenant-Slug"] = slug
    return client


async def _type(client):
    r = await client.post(f"{A}/event-types", json={"name": "Major", "default_duration_hours": 2})
    return r.json()["id"]


def _body(type_id, **kw):
    start = datetime.now(timezone.utc).date() + timedelta(days=2)
    body = {"type_id": type_id, "name": "Bear Hunt", "start_time_utc": "19:00", "anchor_date": str(start),
            "reminder_minutes": [60], "recurrence_kind": "none"}
    body.update(kw)
    return body


class TestServersAndSecondaries:
    async def test_a_server_needs_a_kingdom(self, client, tenant):
        assert (await client.post(f"{A}/discord-servers", json={"name": "X", "guild_id": "gx"})).status_code == 422

    async def test_secondary_servers_are_set_and_listed(self, client, tenant, second_tenant):
        r = await client.patch(f"{A}/tenants/{tenant['id']}", json={"secondary_server_ids": [second_tenant["server_id"]]})
        assert r.status_code == 200, r.text
        assert [s["id"] for s in r.json()["secondary_servers"]] == [second_tenant["server_id"]]
        listing = (await client.get(f"{A}/tenants")).json()
        assert next(t for t in listing if t["slug"] == "mod")["secondary_servers"][0]["name"] == "NSR's server"

    async def test_secondary_cannot_repeat_the_primary(self, client, tenant):
        r = await client.patch(f"{A}/tenants/{tenant['id']}", json={"secondary_server_ids": [tenant["server_id"]]})
        assert r.status_code == 422

    async def test_servers_from_another_kingdom_are_rejected(self, client, tenant, db_session):
        from models.db import DiscordServer
        k2 = Kingdom(name="K2", slug="k2")
        db_session.add(k2)
        await db_session.flush()
        far = DiscordServer(kingdom_id=k2.id, name="Far", guild_id="g-far")
        db_session.add(far)
        await db_session.commit()
        r = await client.patch(f"{A}/tenants/{tenant['id']}", json={"secondary_server_ids": [far.id]})
        assert r.status_code == 422 and "different Kingdom" in r.json()["detail"]
        r = await client.patch(f"{A}/tenants/{tenant['id']}", json={"server_id": far.id})
        assert r.status_code == 422


class TestDestinationCrud:
    async def test_owner_creates_edits_and_deletes(self, client, tenant):
        body = {"label": "Notifications", "server_id": tenant["server_id"], "channel_id": "123", "role_id": "456"}
        created = await client.post(f"{A}/destinations", json=body)
        assert created.status_code == 201, created.text
        dest = created.json()
        assert dest["post_by_default"] is True and dest["leadership_only"] is False and dest["alliance"] == "MOD"
        patched = await client.patch(f"{A}/destinations/{dest['id']}", json={"post_by_default": False, "role_id": ""})
        assert patched.json()["post_by_default"] is False and patched.json()["role_id"] == ""
        assert (await client.delete(f"{A}/destinations/{dest['id']}")).status_code == 204
        assert (await client.get(f"{A}/destinations")).json() == []

    async def test_ids_must_be_digits_and_labels_unique(self, client, tenant):
        base = {"label": "A", "server_id": tenant["server_id"], "channel_id": "1"}
        assert (await client.post(f"{A}/destinations", json={**base, "channel_id": "#general"})).status_code == 422
        assert (await client.post(f"{A}/destinations", json=base)).status_code == 201
        dup = await client.post(f"{A}/destinations", json={**base, "channel_id": "2"})
        assert dup.status_code == 422 and "label" in dup.json()["detail"]

    async def test_server_must_be_primary_or_secondary(self, client, tenant, second_tenant):
        body = {"label": "A", "server_id": second_tenant["server_id"], "channel_id": "1"}
        r = await client.post(f"{A}/destinations", json=body)
        assert r.status_code == 422 and "primary or a secondary" in r.json()["detail"]
        with_secondary = await client.patch(
            f"{A}/tenants/{tenant['id']}", json={"secondary_server_ids": [second_tenant["server_id"]]})
        assert with_secondary.status_code == 200, with_secondary.text
        assert (await client.post(f"{A}/destinations", json=body)).status_code == 201

    async def test_coordinator_cannot_manage_destinations(self, tenant, make_user_and_client):
        c, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "coordinator")])
        _hdr(c, "mod")
        body = {"label": "A", "server_id": tenant["server_id"], "channel_id": "1"}
        assert (await c.post(f"{A}/destinations", json=body)).status_code == 403
        assert (await c.get(f"{A}/destinations")).status_code == 200

    async def test_owner_cannot_touch_another_alliances_destination(self, client, tenant, second_tenant):
        other = await add_destination_api(client, "nsr", second_tenant, "9")
        _hdr(client, "mod")
        assert (await client.delete(f"{A}/destinations/{other}")).status_code == 404

    async def test_creating_a_default_destination_adds_reminders_to_existing_events(self, client, tenant, sf):
        type_id = await _type(client)
        event = (await client.post(f"{A}/events", json=_body(type_id))).json()
        before = await deliveries(sf, event["id"], kind="reminder")
        assert before[0].detail == "No destination configured for this alliance" or before[0].destination_id is None
        await client.post(f"{A}/destinations", json={"label": "N", "server_id": tenant["server_id"], "channel_id": "1"})
        after = [d for d in await deliveries(sf, event["id"], kind="reminder") if d.destination_id]
        assert len(after) == 1 and after[0].status == "pending"

    async def test_delete_is_blocked_while_an_event_or_group_uses_it(self, client, tenant):
        extra = await add_destination_api(client, "mod", tenant, "5", default=False, label="Extra")
        type_id = await _type(client)
        event = (await client.post(f"{A}/events", json=_body(
            type_id, destination_changes=[{"destination_id": extra, "included": True}]))).json()
        r = await client.delete(f"{A}/destinations/{extra}")
        assert r.status_code == 409 and "Bear Hunt" in r.json()["detail"]
        await client.delete(f"{A}/events/{event['id']}")
        group = (await client.post(f"{A}/audience-groups", json={"name": "G", "destination_ids": [extra]})).json()
        r = await client.delete(f"{A}/destinations/{extra}")
        assert r.status_code == 409 and 'group "G"' in r.json()["detail"]
        await client.delete(f"{A}/audience-groups/{group['id']}")
        assert (await client.delete(f"{A}/destinations/{extra}")).status_code == 204


async def add_destination_api(client, slug, tenant, channel, *, default=True, label="Notifications", leadership=False):
    _hdr(client, slug)
    r = await client.post(f"{A}/destinations", json={
        "label": label, "server_id": tenant["server_id"], "channel_id": channel,
        "post_by_default": default, "leadership_only": leadership})
    assert r.status_code == 201, r.text
    return r.json()["id"]


class TestAudienceGroups:
    async def test_a_kingdom_coordinator_manages_groups(self, tenant, second_tenant, make_user_and_client, client):
        dest = await add_destination_api(client, "mod", tenant, "1")
        c, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "coordinator")], kingdom_grants=[tenant["kingdom_id"]])
        _hdr(c, "mod")
        r = await c.post(f"{A}/audience-groups", json={"name": "Leaders", "description": "d", "destination_ids": [dest]})
        assert r.status_code == 201, r.text
        group = r.json()
        assert group["destinations"][0]["alliance"] == "MOD"
        patched = await c.patch(f"{A}/audience-groups/{group['id']}", json={"destination_ids": []})
        assert patched.json()["destination_ids"] == []
        assert [g["name"] for g in (await c.get(f"{A}/audience-groups")).json()] == ["Leaders"]
        assert (await c.delete(f"{A}/audience-groups/{group['id']}")).status_code == 204

    async def test_without_the_kingdom_grant_it_is_forbidden(self, tenant, make_user_and_client):
        c, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "owner")])
        _hdr(c, "mod")
        assert (await c.post(f"{A}/audience-groups", json={"name": "G"})).status_code == 403
        assert (await c.get(f"{A}/audience-groups")).status_code == 200

    async def test_a_viewer_cannot_manage_groups(self, tenant, make_user_and_client):
        c, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")], kingdom_grants=[tenant["kingdom_id"]])
        _hdr(c, "mod")
        assert (await c.post(f"{A}/audience-groups", json={"name": "G"})).status_code == 403

    async def test_duplicate_names_and_foreign_destinations_are_422(self, client, tenant, db_session):
        assert (await client.post(f"{A}/audience-groups", json={"name": "G"})).status_code == 201
        assert (await client.post(f"{A}/audience-groups", json={"name": "G"})).status_code == 422
        assert (await client.post(f"{A}/audience-groups", json={"name": "H", "destination_ids": [999]})).status_code == 422

    async def test_delete_is_blocked_while_an_event_uses_the_group(self, client, tenant):
        group = (await client.post(f"{A}/audience-groups", json={"name": "G"})).json()
        type_id = await _type(client)
        await client.post(f"{A}/events", json=_body(type_id, group_ids=[group["id"]]))
        r = await client.delete(f"{A}/audience-groups/{group['id']}")
        assert r.status_code == 409 and "Bear Hunt" in r.json()["detail"]


class TestEventSelection:
    async def test_overview_counts_destinations_and_merged_channels(self, client, tenant, second_tenant):
        mod = await add_destination_api(client, "mod", tenant, "555")
        # NSR posts to MOD's channel through a secondary server grant
        await client.patch(f"{A}/tenants/{second_tenant['id']}", json={"secondary_server_ids": [tenant["server_id"]]})
        _hdr(client, "nsr")
        r = await client.post(f"{A}/destinations", json={"label": "N", "server_id": tenant["server_id"], "channel_id": "555"})
        assert r.status_code == 201 and mod
        _hdr(client, "mod")
        type_id = await _type(client)
        event = (await client.post(f"{A}/events", json=_body(type_id, alliances=[{"tenant_slug": "nsr"}]))).json()
        assert event["destination_count"] == 2 and event["channel_count"] == 1 and event["warnings"] == []

    async def test_no_destination_is_a_warning(self, client, tenant):
        type_id = await _type(client)
        event = (await client.post(f"{A}/events", json=_body(type_id))).json()
        assert event["destination_count"] == 0 and "no destination" in event["warnings"][0]

    async def test_conflicting_overrides_on_a_shared_channel_are_rejected_at_save(self, client, tenant, second_tenant):
        await add_destination_api(client, "mod", tenant, "555")
        await client.patch(f"{A}/tenants/{second_tenant['id']}", json={"secondary_server_ids": [tenant["server_id"]]})
        _hdr(client, "nsr")
        await client.post(f"{A}/destinations", json={"label": "N", "server_id": tenant["server_id"], "channel_id": "555"})
        _hdr(client, "mod")
        type_id = await _type(client)
        r = await client.post(f"{A}/events", json=_body(type_id, message="Base", alliances=[
            {"tenant_slug": "nsr", "message_override": "NSR wording"}]))
        assert r.status_code == 422
        assert "MOD" in r.json()["detail"] and "NSR" in r.json()["detail"] and "555" in r.json()["detail"]
        assert (await client.get(f"{A}/events")).json() == []

    async def test_a_destination_outside_the_audience_is_rejected(self, client, tenant, second_tenant):
        theirs = await add_destination_api(client, "nsr", second_tenant, "9", default=False, label="Theirs")
        _hdr(client, "mod")
        type_id = await _type(client)
        r = await client.post(f"{A}/events", json=_body(
            type_id, destination_changes=[{"destination_id": theirs, "included": True}]))
        assert r.status_code == 422 and "not in this event's audience" in r.json()["detail"]
        group = (await client.post(f"{A}/audience-groups", json={"name": "G", "destination_ids": [theirs]})).json()
        ok = await client.post(f"{A}/events", json=_body(
            type_id, group_ids=[group["id"]], destination_changes=[{"destination_id": theirs, "included": True}]))
        assert ok.status_code == 201, ok.text

    async def test_leadership_compatibility_is_enforced(self, client, tenant):
        lead = await add_destination_api(client, "mod", tenant, "7", default=False, label="Leaders", leadership=True)
        type_id = await _type(client)
        r = await client.post(f"{A}/events", json=_body(type_id, destination_changes=[{"destination_id": lead, "included": True}]))
        assert r.status_code == 422 and "leadership" in r.json()["detail"]
        ok = await client.post(f"{A}/events", json=_body(
            type_id, leadership_only=True, destination_changes=[{"destination_id": lead, "included": True}]))
        assert ok.status_code == 201 and ok.json()["destination_count"] == 1

    async def test_an_unknown_group_is_rejected(self, client, tenant):
        type_id = await _type(client)
        assert (await client.post(f"{A}/events", json=_body(type_id, group_ids=[42]))).status_code == 422

    async def test_an_event_edit_that_would_conflict_is_rejected_and_changes_nothing(self, client, tenant, second_tenant):
        await add_destination_api(client, "mod", tenant, "555")
        await client.patch(f"{A}/tenants/{second_tenant['id']}", json={"secondary_server_ids": [tenant["server_id"]]})
        _hdr(client, "nsr")
        await client.post(f"{A}/destinations", json={"label": "N", "server_id": tenant["server_id"], "channel_id": "555"})
        _hdr(client, "mod")
        type_id = await _type(client)
        event = (await client.post(f"{A}/events", json=_body(type_id, message="Base", alliances=[{"tenant_slug": "nsr"}]))).json()
        r = await client.patch(f"{A}/events/{event['id']}", json={
            "alliances": [{"tenant_slug": "nsr", "message_override": "Different"}, {"tenant_slug": "mod"}]})
        assert r.status_code == 422
        current = (await client.get(f"{A}/events/{event['id']}")).json()
        assert all(a["message_override"] is None for a in current["alliances"])


class TestPreview:
    async def test_preview_lists_destinations_merges_and_conflicts(self, client, tenant, second_tenant):
        await add_destination_api(client, "mod", tenant, "555")
        await client.patch(f"{A}/tenants/{second_tenant['id']}", json={"secondary_server_ids": [tenant["server_id"]]})
        _hdr(client, "nsr")
        await client.post(f"{A}/destinations", json={"label": "N", "server_id": tenant["server_id"], "channel_id": "555"})
        _hdr(client, "mod")
        r = await client.post(f"{A}/events/preview-destinations", json={
            "alliances": [{"tenant_slug": "nsr", "message_override": "Other"}], "message": "Base"})
        body = r.json()
        assert r.status_code == 200
        assert body["channel_count"] == 1 and len(body["destinations"]) == 2
        assert body["destinations"][0]["shares_channel_with"]
        assert body["conflicts"] and "555" in body["conflicts"][0]

    async def test_preview_requires_write_access(self, tenant, make_user_and_client):
        c, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")])
        _hdr(c, "mod")
        assert (await c.post(f"{A}/events/preview-destinations", json={})).status_code == 403


class TestGroupedDeliveryLog:
    async def _merged(self, client, tenant, second_tenant, sf, fake):
        await add_destination_api(client, "mod", tenant, "555")
        await client.patch(f"{A}/tenants/{second_tenant['id']}", json={"secondary_server_ids": [tenant["server_id"]]})
        _hdr(client, "nsr")
        await client.post(f"{A}/destinations", json={"label": "N", "server_id": tenant["server_id"], "channel_id": "555"})
        _hdr(client, "mod")
        type_id = await _type(client)
        event = (await client.post(f"{A}/events", json=_body(type_id, duration_hours=None, alliances=[{"tenant_slug": "nsr"}]))).json()
        start = datetime.now(timezone.utc).date() + timedelta(days=2)
        await run_delivery_tick(sf, fake, datetime(start.year, start.month, start.day, 18, 0, 30, tzinfo=timezone.utc))
        return event

    async def test_one_line_per_send_with_members(self, client, tenant, second_tenant, sf, fake):
        await self._merged(client, tenant, second_tenant, sf, fake)
        rows = (await client.get(f"{A}/deliveries?days=30", headers={"X-Tenant-Slug": "*"})).json()
        reminders = [r for r in rows if r["kind"] == "reminder"]
        assert len(reminders) == 1
        entry = reminders[0]
        assert entry["status"] == "posted" and entry["alliance_names"] == ["MOD", "NSR"]
        assert len(entry["members"]) == 1 and entry["members"][0]["status"] == "cancelled"

    async def test_the_member_alliance_still_sees_the_send(self, client, tenant, second_tenant, sf, fake):
        await self._merged(client, tenant, second_tenant, sf, fake)
        rows = (await client.get(f"{A}/deliveries?days=30", headers={"X-Tenant-Slug": "nsr"})).json()
        assert [r["status"] for r in rows if r["kind"] == "reminder"] == ["posted"]

    async def test_health_does_not_count_merged_rows_as_cancelled(self, client, tenant, second_tenant, sf, fake):
        await self._merged(client, tenant, second_tenant, sf, fake)
        async with sf() as db:
            for d in (await db.execute(select(Delivery))).scalars():
                d.due_at_utc = datetime.now(timezone.utc) - timedelta(hours=1)
            await db.commit()
        health = (await client.get(f"{A}/delivery-health", headers={"X-Tenant-Slug": "*"})).json()
        assert health["counts"].get("cancelled", 0) == 0 and health["counts"]["posted"] == 1
