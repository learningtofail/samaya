"""Spec §67 and §68: servers with a Kingdom, secondary servers, Audiences and
their destinations, alliance links, event selection, preview and the grouped
delivery log."""
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


class TestAudienceCrud:
    async def test_coordinator_creates_edits_and_deletes(self, client, tenant, second_tenant):
        body = {"label": "Notifications", "leadership_only": False, "links": [{"tenant_id": tenant["id"]}],
                "destinations": [{"server_id": tenant["server_id"], "channel_id": "123", "role_id": "456"}]}
        created = await client.post(f"{A}/audiences", json=body)
        assert created.status_code == 201, created.text
        audience = created.json()
        assert audience["leadership_only"] is False and audience["links"][0]["alliance"] == "MOD"
        assert audience["links"][0]["post_by_default"] is True
        first_id = audience["destinations"][0]["id"]
        patched = await client.patch(f"{A}/audiences/{audience['id']}", json={"destinations": [
            {"server_id": tenant["server_id"], "channel_id": "123", "role_id": "456"},
            {"server_id": second_tenant["server_id"], "channel_id": "789"},
        ]})
        assert patched.status_code == 200, patched.text
        destinations = patched.json()["destinations"]
        assert [d["channel_id"] for d in destinations] == ["123", "789"]
        assert destinations[0]["id"] == first_id  # an unchanged destination keeps its row
        removed = await client.patch(f"{A}/audiences/{audience['id']}", json={"destinations": [
            {"server_id": second_tenant["server_id"], "channel_id": "789"}]})
        assert [d["channel_id"] for d in removed.json()["destinations"]] == ["789"]
        assert (await client.delete(f"{A}/audiences/{audience['id']}")).status_code == 409  # still linked to MOD
        await client.patch(f"{A}/audiences/{audience['id']}", json={"links": []})
        assert (await client.delete(f"{A}/audiences/{audience['id']}")).status_code == 204
        assert (await client.get(f"{A}/audiences")).json() == []

    async def test_ids_must_be_digits_labels_unique_and_a_destination_required(self, client, tenant):
        dest = {"server_id": tenant["server_id"], "channel_id": "1"}
        base = {"label": "A", "destinations": [dest]}
        assert (await client.post(f"{A}/audiences", json={**base, "destinations": [{**dest, "channel_id": "#general"}]})).status_code == 422
        assert (await client.post(f"{A}/audiences", json={"label": "B", "destinations": []})).status_code == 422
        assert (await client.post(f"{A}/audiences", json=base)).status_code == 201
        dup = await client.post(f"{A}/audiences", json={**base, "destinations": [{**dest, "channel_id": "2"}]})
        assert dup.status_code == 422 and "label" in dup.json()["detail"]

    async def test_servers_must_belong_to_the_kingdom_but_need_not_be_the_alliances(self, client, tenant, second_tenant, db_session):
        from models.db import DiscordServer
        k2 = Kingdom(name="K2", slug="k2")
        db_session.add(k2)
        await db_session.flush()
        far = DiscordServer(kingdom_id=k2.id, name="Far", guild_id="g-far")
        db_session.add(far)
        await db_session.commit()
        r = await client.post(f"{A}/audiences", json={"label": "A", "destinations": [{"server_id": far.id, "channel_id": "1"}]})
        assert r.status_code == 422 and "Kingdom" in r.json()["detail"]
        other_alliance_server = {"server_id": second_tenant["server_id"], "channel_id": "1"}
        assert (await client.post(f"{A}/audiences", json={"label": "A", "destinations": [other_alliance_server]})).status_code == 201

    async def test_an_alliance_owner_cannot_manage_audiences(self, tenant, make_user_and_client):
        c, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "owner")])
        _hdr(c, "mod")
        body = {"label": "A", "destinations": [{"server_id": tenant["server_id"], "channel_id": "1"}]}
        assert (await c.post(f"{A}/audiences", json=body)).status_code == 403
        assert (await c.get(f"{A}/audiences")).status_code == 200

    async def test_a_kingdom_coordinator_can(self, tenant, make_user_and_client):
        c, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "coordinator")], kingdom_grants=[tenant["kingdom_id"]])
        _hdr(c, "mod")
        body = {"label": "A", "destinations": [{"server_id": tenant["server_id"], "channel_id": "1"}]}
        assert (await c.post(f"{A}/audiences", json=body)).status_code == 201

    async def test_linking_a_default_audience_adds_reminders_to_existing_events(self, client, tenant, sf):
        type_id = await _type(client)
        event = (await client.post(f"{A}/events", json=_body(type_id))).json()
        before = await deliveries(sf, event["id"], kind="reminder")
        assert before[0].destination_id is None
        await add_audience_api(client, "mod", tenant, "1")
        after = [d for d in await deliveries(sf, event["id"], kind="reminder") if d.destination_id]
        assert len(after) == 1 and after[0].status == "pending"

    async def test_delete_is_blocked_while_an_alliance_or_event_uses_it(self, client, tenant):
        extra = await add_audience_api(client, "mod", tenant, "5", default=False, label="Extra")
        r = await client.delete(f"{A}/audiences/{extra}")
        assert r.status_code == 409 and 'alliance "MOD"' in r.json()["detail"]
        type_id = await _type(client)
        event = (await client.post(f"{A}/events", json=_body(
            type_id, audience_changes=[{"audience_id": extra, "included": True}]))).json()
        await client.put(f"{A}/alliance-audiences", json={"links": []})  # unlinked, but the event still adds it
        r = await client.delete(f"{A}/audiences/{extra}")
        assert r.status_code == 409 and "Bear Hunt" in r.json()["detail"]
        await client.delete(f"{A}/events/{event['id']}")
        assert (await client.delete(f"{A}/audiences/{extra}")).status_code == 204


async def add_audience_api(client, slug, tenant, channel, *, default=True, label="Notifications", leadership=False,
                           also_link=()):
    """Creates an Audience with one destination, linked to `tenant` (and the tenant ids in `also_link`)."""
    _hdr(client, slug)
    links = [{"tenant_id": tenant["id"], "post_by_default": default}, *[{"tenant_id": t, "post_by_default": default} for t in also_link]]
    r = await client.post(f"{A}/audiences", json={
        "label": label, "leadership_only": leadership, "links": links,
        "destinations": [{"server_id": tenant["server_id"], "channel_id": channel}]})
    assert r.status_code == 201, r.text
    return r.json()["id"]


class TestAllianceLinks:
    async def test_an_owner_chooses_their_alliances_audiences(self, client, tenant, make_user_and_client):
        shared = await add_audience_api(client, "mod", tenant, "1", default=False, label="Shared")
        c, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "owner")])
        _hdr(c, "mod")
        r = await c.put(f"{A}/alliance-audiences", json={"links": [{"audience_id": shared, "post_by_default": True}]})
        assert r.status_code == 200, r.text
        listing = (await c.get(f"{A}/audiences")).json()
        assert listing[0]["links"][0]["post_by_default"] is True

    async def test_a_viewer_or_coordinator_without_the_kingdom_grant_cannot(self, client, tenant, make_user_and_client):
        shared = await add_audience_api(client, "mod", tenant, "1", label="Shared")
        for role in ("viewer", "coordinator"):
            c, _ = await make_user_and_client(tenant_grants=[(tenant["id"], role)], discord_id=f"d-{role}")
            _hdr(c, "mod")
            r = await c.put(f"{A}/alliance-audiences", json={"links": [{"audience_id": shared}]})
            assert r.status_code == 403, role

    async def test_an_unknown_audience_is_422(self, client, tenant):
        _hdr(client, "mod")
        assert (await client.put(f"{A}/alliance-audiences", json={"links": [{"audience_id": 999}]})).status_code == 422

    async def test_the_default_flag_decides_whether_events_post_there(self, client, tenant, sf):
        optional = await add_audience_api(client, "mod", tenant, "4242", default=False, label="Optional")
        type_id = await _type(client)
        plain = (await client.post(f"{A}/events", json=_body(type_id))).json()
        assert plain["destination_count"] == 0
        chosen = (await client.post(f"{A}/events", json=_body(
            type_id, name="Chosen", audience_changes=[{"audience_id": optional, "included": True}]))).json()
        assert chosen["audience_count"] == 1 and chosen["destination_count"] == 1


class TestEventSelection:
    async def test_overview_counts_audiences_destinations_and_merged_channels(self, client, tenant, second_tenant):
        await add_audience_api(client, "mod", tenant, "555", label="Shared", also_link=[second_tenant["id"]])
        await add_audience_api(client, "mod", tenant, "555", label="Same channel again")
        type_id = await _type(client)
        event = (await client.post(f"{A}/events", json=_body(type_id, alliances=[{"tenant_slug": "nsr"}]))).json()
        assert event["audience_count"] == 2 and event["destination_count"] == 2
        assert event["channel_count"] == 1 and event["warnings"] == []

    async def test_one_audience_with_destinations_on_two_servers_is_two_channels(self, client, tenant, second_tenant):
        r = await client.post(f"{A}/audiences", json={
            "label": "Everywhere", "links": [{"tenant_id": tenant["id"]}], "destinations": [
                {"server_id": tenant["server_id"], "channel_id": "1"},
                {"server_id": second_tenant["server_id"], "channel_id": "2"}]})
        assert r.status_code == 201, r.text
        type_id = await _type(client)
        event = (await client.post(f"{A}/events", json=_body(type_id))).json()
        assert event["audience_count"] == 1 and event["destination_count"] == 2 and event["channel_count"] == 2

    async def test_no_audience_is_a_warning(self, client, tenant):
        type_id = await _type(client)
        event = (await client.post(f"{A}/events", json=_body(type_id))).json()
        assert event["destination_count"] == 0 and "no audience" in event["warnings"][0]

    async def test_conflicting_overrides_on_a_shared_channel_are_rejected_at_save(self, client, tenant, second_tenant):
        await add_audience_api(client, "mod", tenant, "555", also_link=[second_tenant["id"]])
        type_id = await _type(client)
        r = await client.post(f"{A}/events", json=_body(type_id, message="Base", alliances=[
            {"tenant_slug": "nsr", "message_override": "NSR wording"}]))
        assert r.status_code == 422
        assert "MOD" in r.json()["detail"] and "NSR" in r.json()["detail"] and "555" in r.json()["detail"]
        assert (await client.get(f"{A}/events")).json() == []

    async def test_an_audience_no_alliance_in_the_event_uses_is_rejected_until_linked(self, client, tenant, second_tenant):
        theirs = await add_audience_api(client, "nsr", second_tenant, "9", default=False, label="Theirs")
        _hdr(client, "mod")
        type_id = await _type(client)
        change = [{"audience_id": theirs, "included": True}]
        r = await client.post(f"{A}/events", json=_body(type_id, audience_changes=change))
        assert r.status_code == 422 and "not used by any alliance" in r.json()["detail"]
        link = await client.put(f"{A}/alliance-audiences", json={"links": [{"audience_id": theirs, "post_by_default": False}]})
        assert link.status_code == 200, link.text
        ok = await client.post(f"{A}/events", json=_body(type_id, audience_changes=change))
        assert ok.status_code == 201, ok.text

    async def test_leadership_compatibility_is_enforced(self, client, tenant):
        lead = await add_audience_api(client, "mod", tenant, "7", default=False, label="Leaders", leadership=True)
        type_id = await _type(client)
        r = await client.post(f"{A}/events", json=_body(type_id, audience_changes=[{"audience_id": lead, "included": True}]))
        assert r.status_code == 422 and "leadership" in r.json()["detail"]
        ok = await client.post(f"{A}/events", json=_body(
            type_id, leadership_only=True, audience_changes=[{"audience_id": lead, "included": True}]))
        assert ok.status_code == 201 and ok.json()["destination_count"] == 1

    async def test_an_event_edit_that_would_conflict_is_rejected_and_changes_nothing(self, client, tenant, second_tenant):
        await add_audience_api(client, "mod", tenant, "555", also_link=[second_tenant["id"]])
        type_id = await _type(client)
        event = (await client.post(f"{A}/events", json=_body(type_id, message="Base", alliances=[{"tenant_slug": "nsr"}]))).json()
        r = await client.patch(f"{A}/events/{event['id']}", json={
            "alliances": [{"tenant_slug": "nsr", "message_override": "Different"}, {"tenant_slug": "mod"}]})
        assert r.status_code == 422
        current = (await client.get(f"{A}/events/{event['id']}")).json()
        assert all(a["message_override"] is None for a in current["alliances"])


class TestPreview:
    async def test_preview_lists_audiences_merges_and_conflicts(self, client, tenant, second_tenant):
        await add_audience_api(client, "mod", tenant, "555", label="One", also_link=[second_tenant["id"]])
        await add_audience_api(client, "mod", tenant, "555", label="Two")
        r = await client.post(f"{A}/events/preview-destinations", json={
            "alliances": [{"tenant_slug": "nsr", "message_override": "Other"}], "message": "Base"})
        body = r.json()
        assert r.status_code == 200
        assert body["channel_count"] == 1 and len(body["audiences"]) == 2
        assert body["audiences"][0]["destinations"][0]["channel_id"] == "555"
        assert body["audiences"][0]["shares_channel_with"] == ["Two"]
        assert body["conflicts"] and "555" in body["conflicts"][0]

    async def test_preview_requires_write_access(self, tenant, make_user_and_client):
        c, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")])
        _hdr(c, "mod")
        assert (await c.post(f"{A}/events/preview-destinations", json={})).status_code == 403


class TestGroupedDeliveryLog:
    async def _merged(self, client, tenant, second_tenant, sf, fake):
        await add_audience_api(client, "mod", tenant, "555", also_link=[second_tenant["id"]])
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
