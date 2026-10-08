"""Spec §87: the admin API for attendance groups and Notify me roles, the event fields, series splits, deleting
an event, the occurrence headcount, and the signed webhook route end to end."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from nacl.signing import SigningKey
from sqlalchemy import select

from models import get_session_factory
from models.db import (
    AuditLog, DiscordServer, Event, EventSubscription, Kingdom, OccurrenceRsvp, SignupGroup, SignupRole,
)
from routers import webhooks
from services.discord_client import get_discord
from services.signup import rsvp_custom_id, sub_custom_id, sub_hash
from tests.unified_helpers import make_event, sync

A = "/admin/api"
H = {"X-Tenant-Slug": "mod"}
GUILD = "test-guild-mod"
UTC = timezone.utc
TODAY = date.today()
SOON = TODAY + timedelta(days=3)
USER = "123456789012345678"


@pytest.fixture
def discord(fake):
    from main import app
    app.dependency_overrides[get_discord] = lambda: fake
    fake.add_role(GUILD, "1001", "Bear Hunt")
    fake.add_role(GUILD, "1002", "Bear Hunt #2")
    yield fake
    app.dependency_overrides.pop(get_discord, None)


async def an_event(sf, tenant, name="Bear Hunt", **kw):
    kw.setdefault("anchor", SOON)
    kw.setdefault("interval", None)
    event_id = await make_event(sf, tenant, name=name, **kw)
    await sync(sf, event_id, now=datetime.now(UTC).replace(hour=0, minute=0))
    return event_id


async def group(client, name="Bear Hunt, daily", **body):
    return await client.post(f"{A}/signup-groups", json={"name": name, **body}, headers=H)


async def audit(sf, table):
    async with sf() as s:
        return list((await s.execute(select(AuditLog).where(AuditLog.table_name == table))).scalars())


async def other_kingdom_server(sf):
    async with sf() as s:
        kingdom = Kingdom(name="Other", slug="1006")
        s.add(kingdom)
        await s.flush()
        server = DiscordServer(kingdom_id=kingdom.id, name="Other server", guild_id="other-guild", bot_token="t", public_key="p")
        s.add(server)
        await s.commit()
        return kingdom.id, server.id


# ── Attendance groups ────────────────────────────────────────

class TestGroups:
    async def test_create_list_update_delete(self, client, sf, configured):
        created = await group(client, attendance_window="week")
        assert created.status_code == 201
        body = created.json()
        assert body["name"] == "Bear Hunt, daily" and body["exclusive_roles"] is True and body["attendance_window"] == "week"
        assert [g["id"] for g in (await client.get(f"{A}/signup-groups", headers=H)).json()] == [body["id"]]
        patched = await client.patch(f"{A}/signup-groups/{body['id']}", json={"attendance_window": "day", "exclusive_roles": False}, headers=H)
        assert patched.json()["attendance_window"] == "day" and patched.json()["exclusive_roles"] is False
        event_id = await an_event(sf, configured)
        await client.patch(f"{A}/events/{event_id}", json={"signup_group_id": body["id"]}, headers=H)
        deleted = await client.delete(f"{A}/signup-groups/{body['id']}", headers=H)
        assert deleted.status_code == 200 and deleted.json() == {"released_events": ["Bear Hunt"]}
        async with sf() as s:
            assert (await s.get(Event, event_id)).signup_group_id is None
        assert [a.action for a in await audit(sf, "signup_groups")] == ["create", "update", "delete"]

    async def test_a_duplicate_name_is_a_friendly_422(self, client, configured):
        await group(client)
        response = await group(client)
        assert response.status_code == 422 and "already has an attendance group" in response.json()["detail"]

    @pytest.mark.parametrize("body", [{"name": ""}, {"name": "x" * 81}, {"name": "ok", "attendance_window": "month"}])
    async def test_validation(self, client, configured, body):
        assert (await client.post(f"{A}/signup-groups", json=body, headers=H)).status_code == 422

    async def test_only_a_kingdom_coordinator_writes(self, make_user_and_client, sf, configured):
        owner = await make_user_and_client(tenant_grants=[(configured["id"], "owner")], kingdom_grants=[])
        client2, _ = owner if isinstance(owner, tuple) else (owner, None)
        assert (await group(client2)).status_code == 403
        assert (await client2.get(f"{A}/signup-groups", headers=H)).status_code == 200

    async def test_a_coordinator_can_write(self, make_user_and_client, configured):
        client2 = await make_user_and_client(tenant_grants=[(configured["id"], "coordinator")], kingdom_grants=[configured["kingdom_id"]])
        client2 = client2[0] if isinstance(client2, tuple) else client2
        assert (await group(client2)).status_code == 201

    async def test_a_viewer_cannot_write(self, make_user_and_client, configured):
        client2 = await make_user_and_client(tenant_grants=[(configured["id"], "viewer")], kingdom_grants=[configured["kingdom_id"]])
        client2 = client2[0] if isinstance(client2, tuple) else client2
        assert (await group(client2)).status_code == 403

    async def test_a_group_from_another_kingdom_is_not_found_or_refused(self, client, sf, configured):
        other_kingdom, _ = await other_kingdom_server(sf)
        async with sf() as s:
            foreign = SignupGroup(kingdom_id=other_kingdom, name="Foreign")
            s.add(foreign)
            await s.commit()
            foreign_id = foreign.id
        assert (await client.patch(f"{A}/signup-groups/{foreign_id}", json={"name": "x"}, headers=H)).status_code == 404
        assert (await client.delete(f"{A}/signup-groups/{foreign_id}", headers=H)).status_code == 404
        event_id = await an_event(sf, configured)
        response = await client.patch(f"{A}/events/{event_id}", json={"signup_group_id": foreign_id}, headers=H)
        assert response.status_code == 422 and "Kingdom" in response.json()["detail"]
        assert [g["name"] for g in (await client.get(f"{A}/signup-groups", headers=H)).json()] == []

    async def test_an_event_can_leave_its_group(self, client, sf, configured):
        gid = (await group(client)).json()["id"]
        event_id = await an_event(sf, configured)
        assert (await client.patch(f"{A}/events/{event_id}", json={"signup_group_id": gid}, headers=H)).json()["signup_group_id"] == gid
        assert (await client.patch(f"{A}/events/{event_id}", json={"signup_group_id": None}, headers=H)).json()["signup_group_id"] is None


# ── Event fields ─────────────────────────────────────────────

class TestEventFields:
    async def _type(self, client):
        return (await client.post(f"{A}/event-types", json={"name": "Major"}, headers=H)).json()["id"]

    def _body(self, type_id, **kw):
        return {"type_id": type_id, "name": "Bear Hunt", "start_time_utc": "19:00", "anchor_date": str(SOON), **kw}

    async def test_new_events_default_to_buttons_on_and_pings_off(self, client, configured):
        body = (await client.post(f"{A}/events", json=self._body(await self._type(client)), headers=H)).json()
        assert body["signup_enabled"] is True and body["rsvp_enabled"] is True and body["signup_mention"] is False

    async def test_buttons_can_be_turned_off_on_create_and_update(self, client, configured):
        type_id = await self._type(client)
        body = (await client.post(f"{A}/events", json=self._body(type_id, signup_enabled=False, rsvp_enabled=False, signup_mention=True), headers=H)).json()
        assert (body["signup_enabled"], body["rsvp_enabled"], body["signup_mention"]) == (False, False, True)
        back = (await client.patch(f"{A}/events/{body['id']}", json={"signup_enabled": True, "rsvp_enabled": True}, headers=H)).json()
        assert back["signup_enabled"] is True and back["rsvp_enabled"] is True

    async def test_a_leadership_only_event_cannot_ask_for_buttons(self, client, configured):
        type_id = await self._type(client)
        for field in ("signup_enabled", "rsvp_enabled"):
            response = await client.post(f"{A}/events", json=self._body(type_id, leadership_only=True, **{field: True}), headers=H)
            assert response.status_code == 422 and "leadership-only" in response.json()["detail"]

    async def test_a_leadership_only_event_quietly_has_them_off(self, client, configured, sf):
        from tests.unified_helpers import add_audience
        await add_audience(sf, configured, "chan-lead", "", label="Leaders", leadership=True)
        body = (await client.post(f"{A}/events", json=self._body(await self._type(client), leadership_only=True), headers=H)).json()
        assert body["signup_enabled"] is False and body["rsvp_enabled"] is False

    async def test_making_an_event_leadership_only_turns_them_off(self, client, configured, sf):
        from tests.unified_helpers import add_audience
        await add_audience(sf, configured, "chan-lead", "", label="Leaders", leadership=True)
        body = (await client.post(f"{A}/events", json=self._body(await self._type(client)), headers=H)).json()
        patched = (await client.patch(f"{A}/events/{body['id']}", json={"leadership_only": True}, headers=H)).json()
        assert patched["signup_enabled"] is False and patched["rsvp_enabled"] is False
        assert (await client.patch(f"{A}/events/{body['id']}", json={"leadership_only": True, "rsvp_enabled": True}, headers=H)).status_code == 422

    async def test_changes_are_audited(self, client, configured, sf):
        body = (await client.post(f"{A}/events", json=self._body(await self._type(client)), headers=H)).json()
        await client.patch(f"{A}/events/{body['id']}", json={"signup_mention": True}, headers=H)
        last = (await audit(sf, "events"))[-1]
        assert json.loads(last.before)["signup_mention"] is False and json.loads(last.after)["signup_mention"] is True


# ── Role mappings ────────────────────────────────────────────

class TestEventRoles:
    async def test_linking_a_safe_role(self, client, sf, configured, discord):
        event_id = await an_event(sf, configured)
        response = await client.put(f"{A}/events/{event_id}/signup-roles", json={"server_id": configured["server_id"], "role_id": "1001"}, headers=H)
        assert response.status_code == 200
        assert response.json() == {"role_id": "1001", "role_name": "Bear Hunt", "created_by_samaya": False}
        async with sf() as s:
            row = (await s.execute(select(SignupRole))).scalar_one()
            assert row.event_id == event_id and row.type_id is None
        assert [a.action for a in await audit(sf, "signup_roles")] == ["create"]

    @pytest.mark.parametrize("kwargs,word", [
        ({"permissions": 1 << 3}, "Administrator"), ({"permissions": 1 << 13}, "Manage Messages"),
        ({"managed": True}, "managed"), ({"position": 100}, "highest role")])
    async def test_an_unsafe_role_is_a_422_naming_the_reason(self, client, sf, configured, discord, kwargs, word):
        discord.add_role(GUILD, "1003", "Risky", **kwargs)
        event_id = await an_event(sf, configured)
        response = await client.put(f"{A}/events/{event_id}/signup-roles", json={"server_id": configured["server_id"], "role_id": "1003"}, headers=H)
        assert response.status_code == 422 and word in response.json()["detail"]
        async with sf() as s:
            assert (await s.execute(select(SignupRole))).first() is None

    async def test_everyone_and_unknown_roles_are_refused(self, client, sf, configured, discord):
        event_id = await an_event(sf, configured)
        for role in (GUILD, "9999"):
            response = await client.put(f"{A}/events/{event_id}/signup-roles", json={"server_id": configured["server_id"], "role_id": role}, headers=H)
            assert response.status_code == 422

    async def test_creating_a_role_named_after_the_event(self, client, sf, configured, discord):
        event_id = await an_event(sf, configured, name="Bear Hunt #3")
        response = await client.put(f"{A}/events/{event_id}/signup-roles", json={"server_id": configured["server_id"], "create": True}, headers=H)
        assert response.status_code == 200 and response.json()["role_name"] == "Bear Hunt #3" and response.json()["created_by_samaya"] is True
        assert ("create_role", GUILD, "Bear Hunt #3") in discord.calls
        role = discord.guild_roles[GUILD][response.json()["role_id"]]
        assert role["permissions"] == "0"

    async def test_an_existing_role_with_that_name_is_never_duplicated(self, client, sf, configured, discord):
        event_id = await an_event(sf, configured, name="Bear Hunt")
        response = await client.put(f"{A}/events/{event_id}/signup-roles", json={"server_id": configured["server_id"], "create": True}, headers=H)
        assert response.status_code == 409 and "already exists" in response.json()["detail"]
        assert not [c for c in discord.calls if c[0] == "create_role"]

    async def test_a_second_create_for_the_same_event_makes_no_second_role(self, client, sf, configured, discord):
        event_id = await an_event(sf, configured, name="Bear Hunt #3")
        body = {"server_id": configured["server_id"], "create": True}
        first = await client.put(f"{A}/events/{event_id}/signup-roles", json=body, headers=H)
        second = await client.put(f"{A}/events/{event_id}/signup-roles", json=body, headers=H)
        assert first.status_code == 200 and second.status_code == 409
        assert len([c for c in discord.calls if c[0] == "create_role"]) == 1

    async def test_two_simultaneous_creates_make_one_role(self, client, sf, configured, discord):
        import asyncio
        event_id = await an_event(sf, configured, name="Bear Hunt #4")
        body = {"server_id": configured["server_id"], "create": True}
        results = await asyncio.gather(*[client.put(f"{A}/events/{event_id}/signup-roles", json=body, headers=H) for _ in range(2)])
        assert sorted(r.status_code for r in results) == [200, 409]
        assert len([c for c in discord.calls if c[0] == "create_role"]) == 1

    async def test_the_duplicate_check_reads_the_live_role_list(self, client, sf, configured, discord):
        event_id = await an_event(sf, configured, name="Bear Hunt #5")
        await client.put(f"{A}/events/{event_id}/signup-roles", json={"server_id": configured["server_id"], "create": True}, headers=H)
        assert ("role_context", GUILD) in discord.calls
        assert discord.fresh_reads and discord.fresh_reads[-1] is True

    async def test_a_name_over_the_discord_limit_is_refused(self, client, sf, configured, discord):
        event_id = await an_event(sf, configured, name="x" * 101)
        response = await client.put(f"{A}/events/{event_id}/signup-roles", json={"server_id": configured["server_id"], "create": True}, headers=H)
        assert response.status_code == 422 and "100" in response.json()["detail"]

    async def test_a_bot_without_manage_roles_is_a_502_with_the_reason(self, client, sf, configured, discord):
        discord.create_role_error = "403 Missing permissions — the bot needs Manage Roles"
        event_id = await an_event(sf, configured, name="Brand New")
        response = await client.put(f"{A}/events/{event_id}/signup-roles", json={"server_id": configured["server_id"], "create": True}, headers=H)
        assert response.status_code == 502 and "Manage Roles" in response.json()["detail"]

    async def test_discord_being_unreachable_is_a_502(self, client, sf, configured, discord):
        discord.role_context_error = "Network error: boom"
        event_id = await an_event(sf, configured)
        response = await client.put(f"{A}/events/{event_id}/signup-roles", json={"server_id": configured["server_id"], "role_id": "1001"}, headers=H)
        assert response.status_code == 502

    async def test_exactly_one_action(self, client, sf, configured, discord):
        event_id = await an_event(sf, configured)
        for body in ({"server_id": configured["server_id"]}, {"server_id": configured["server_id"], "create": True, "remove": True},
                     {"server_id": configured["server_id"], "role_id": "abc"}):
            assert (await client.put(f"{A}/events/{event_id}/signup-roles", json=body, headers=H)).status_code == 422

    async def test_removing_a_mapping_deletes_its_subscriptions(self, client, sf, configured, discord):
        event_id = await an_event(sf, configured)
        await client.put(f"{A}/events/{event_id}/signup-roles", json={"server_id": configured["server_id"], "role_id": "1001"}, headers=H)
        async with sf() as s:
            s.add(EventSubscription(server_id=configured["server_id"], role_id="1001", voter_hash=sub_hash(configured["server_id"], "1001", USER)))
            await s.commit()
        assert (await client.put(f"{A}/events/{event_id}/signup-roles", json={"server_id": configured["server_id"], "remove": True}, headers=H)).json() == {"removed": True}
        async with sf() as s:
            assert (await s.execute(select(SignupRole))).first() is None and (await s.execute(select(EventSubscription))).first() is None
        assert (await client.put(f"{A}/events/{event_id}/signup-roles", json={"server_id": configured["server_id"], "remove": True}, headers=H)).status_code == 404

    async def test_subscribers_survive_when_another_mapping_uses_the_role(self, client, sf, configured, discord):
        first, second = await an_event(sf, configured), await an_event(sf, configured, name="Second")
        for event_id in (first, second):
            await client.put(f"{A}/events/{event_id}/signup-roles", json={"server_id": configured["server_id"], "role_id": "1001"}, headers=H)
        async with sf() as s:
            s.add(EventSubscription(server_id=configured["server_id"], role_id="1001", voter_hash="h" * 64))
            await s.commit()
        await client.put(f"{A}/events/{first}/signup-roles", json={"server_id": configured["server_id"], "remove": True}, headers=H)
        async with sf() as s:
            assert len((await s.execute(select(EventSubscription))).scalars().all()) == 1

    async def test_replacing_the_role_drops_the_old_roles_subscribers(self, client, sf, configured, discord):
        event_id = await an_event(sf, configured)
        await client.put(f"{A}/events/{event_id}/signup-roles", json={"server_id": configured["server_id"], "role_id": "1001"}, headers=H)
        async with sf() as s:
            s.add(EventSubscription(server_id=configured["server_id"], role_id="1001", voter_hash="h" * 64))
            await s.commit()
        await client.put(f"{A}/events/{event_id}/signup-roles", json={"server_id": configured["server_id"], "role_id": "1002"}, headers=H)
        async with sf() as s:
            assert (await s.execute(select(EventSubscription))).first() is None
            assert (await s.execute(select(SignupRole.role_id))).scalar_one() == "1002"

    async def test_a_server_from_another_kingdom_is_not_found(self, client, sf, configured, discord):
        _, foreign_server = await other_kingdom_server(sf)
        event_id = await an_event(sf, configured)
        response = await client.put(f"{A}/events/{event_id}/signup-roles", json={"server_id": foreign_server, "role_id": "1001"}, headers=H)
        assert response.status_code == 404

    async def test_a_leadership_only_event_cannot_have_a_role(self, client, sf, configured, discord):
        from tests.unified_helpers import add_audience
        await add_audience(sf, configured, "chan-lead", "", label="Leaders", leadership=True)
        event_id = await an_event(sf, configured, leadership_only=True)
        response = await client.put(f"{A}/events/{event_id}/signup-roles", json={"server_id": configured["server_id"], "role_id": "1001"}, headers=H)
        assert response.status_code == 422

    async def test_two_events_of_one_group_cannot_share_a_role(self, client, sf, configured, discord):
        gid = (await group(client)).json()["id"]
        first, second = await an_event(sf, configured, name="One"), await an_event(sf, configured, name="Two")
        for event_id in (first, second):
            await client.patch(f"{A}/events/{event_id}", json={"signup_group_id": gid}, headers=H)
        body = {"server_id": configured["server_id"], "role_id": "1001"}
        assert (await client.put(f"{A}/events/{first}/signup-roles", json=body, headers=H)).status_code == 200
        response = await client.put(f"{A}/events/{second}/signup-roles", json=body, headers=H)
        assert response.status_code == 422 and "One" in response.json()["detail"]

    async def test_a_group_that_allows_several_roles_can_share(self, client, sf, configured, discord):
        gid = (await group(client, exclusive_roles=False)).json()["id"]
        first, second = await an_event(sf, configured, name="One"), await an_event(sf, configured, name="Two")
        for event_id in (first, second):
            await client.patch(f"{A}/events/{event_id}", json={"signup_group_id": gid}, headers=H)
        body = {"server_id": configured["server_id"], "role_id": "1001"}
        assert (await client.put(f"{A}/events/{first}/signup-roles", json=body, headers=H)).status_code == 200
        assert (await client.put(f"{A}/events/{second}/signup-roles", json=body, headers=H)).status_code == 200

    async def test_a_viewer_cannot_change_a_role(self, make_user_and_client, sf, configured, discord):
        event_id = await an_event(sf, configured)
        client2 = await make_user_and_client(tenant_grants=[(configured["id"], "viewer")], kingdom_grants=[])
        client2 = client2[0] if isinstance(client2, tuple) else client2
        response = await client2.put(f"{A}/events/{event_id}/signup-roles", json={"server_id": configured["server_id"], "role_id": "1001"}, headers=H)
        assert response.status_code == 403

    async def test_an_event_from_another_alliance_is_not_found(self, client, make_user_and_client, sf, configured, second_tenant, discord):
        event_id = await an_event(sf, second_tenant, name="Theirs", audience=[second_tenant["id"]])
        client2 = await make_user_and_client(tenant_grants=[(configured["id"], "owner")], kingdom_grants=[])
        client2 = client2[0] if isinstance(client2, tuple) else client2
        response = await client2.put(f"{A}/events/{event_id}/signup-roles", json={"server_id": configured["server_id"], "role_id": "1001"}, headers=H)
        assert response.status_code == 404


class TestTypeRoles:
    async def _type_id(self, sf, configured):
        event_id = await an_event(sf, configured)
        async with sf() as s:
            return (await s.get(Event, event_id)).type_id

    async def test_a_coordinator_sets_the_type_role_and_it_is_listed(self, client, sf, configured, discord):
        type_id = await self._type_id(sf, configured)
        response = await client.put(f"{A}/event-types/{type_id}/signup-roles", json={"server_id": configured["server_id"], "role_id": "1001"}, headers=H)
        assert response.status_code == 200
        listed = (await client.get(f"{A}/event-types", headers=H)).json()
        roles = next(t for t in listed if t["id"] == type_id)["signup_roles"]
        assert [(r["role_id"], r["scope"], r["subscribers"]) for r in roles] == [("1001", "type", 0)]

    async def test_creating_a_type_role_is_named_after_the_type(self, client, sf, configured, discord):
        type_id = await self._type_id(sf, configured)
        response = await client.put(f"{A}/event-types/{type_id}/signup-roles", json={"server_id": configured["server_id"], "create": True}, headers=H)
        assert response.status_code == 200 and response.json()["role_name"] == "General"

    async def test_an_unsafe_type_role_is_refused(self, client, sf, configured, discord):
        discord.add_role(GUILD, "1003", "Risky", permissions=1 << 28)
        type_id = await self._type_id(sf, configured)
        response = await client.put(f"{A}/event-types/{type_id}/signup-roles", json={"server_id": configured["server_id"], "role_id": "1003"}, headers=H)
        assert response.status_code == 422

    async def test_only_a_kingdom_coordinator_may(self, make_user_and_client, sf, configured, discord):
        type_id = await self._type_id(sf, configured)
        client2 = await make_user_and_client(tenant_grants=[(configured["id"], "owner")], kingdom_grants=[])
        client2 = client2[0] if isinstance(client2, tuple) else client2
        response = await client2.put(f"{A}/event-types/{type_id}/signup-roles", json={"server_id": configured["server_id"], "role_id": "1001"}, headers=H)
        assert response.status_code == 403

    async def test_a_type_from_another_kingdom_is_not_found(self, client, sf, configured, discord):
        response = await client.put(f"{A}/event-types/99999/signup-roles", json={"server_id": configured["server_id"], "role_id": "1001"}, headers=H)
        assert response.status_code == 404


class TestServerRoles:
    async def test_lists_usable_and_unsafe_roles_without_managed_or_everyone(self, client, sf, configured, discord):
        discord.add_role(GUILD, "1010", "Mods", permissions=1 << 13)
        discord.add_role(GUILD, "1011", "Bot role", managed=True)
        discord.add_role(GUILD, GUILD, "@everyone")
        response = await client.get(f"{A}/signup-roles/server-roles", params={"server_id": configured["server_id"]}, headers=H)
        assert response.status_code == 200
        by_id = {r["id"]: r for r in response.json()["roles"]}
        assert set(by_id) == {"1001", "1002", "1010"}
        assert by_id["1001"]["unsafe_reason"] is None and "Manage Messages" in by_id["1010"]["unsafe_reason"]

    async def test_unknown_server_is_not_found(self, client, sf, configured, discord):
        response = await client.get(f"{A}/signup-roles/server-roles", params={"server_id": 99999}, headers=H)
        assert response.status_code == 404

    async def test_discord_failure_is_a_502(self, client, sf, configured, discord):
        discord.role_context_error = "down"
        response = await client.get(f"{A}/signup-roles/server-roles", params={"server_id": configured["server_id"]}, headers=H)
        assert response.status_code == 502


class TestFindAndSyncName:
    async def test_find_lists_unmapped_roles_with_the_events_name(self, client, sf, configured, discord):
        discord.add_role(GUILD, "1004", "Bear Hunt #9")
        discord.add_role(GUILD, "1005", "Bear Hunt #9", permissions=1 << 13)
        discord.add_role(GUILD, "1006", "Something else")
        discord.add_role(GUILD, "1007", "Bear Hunt #9")
        event_id = await an_event(sf, configured, name="Bear Hunt #9")
        other_event = await an_event(sf, configured, name="Elsewhere")
        await client.put(f"{A}/events/{other_event}/signup-roles", json={"server_id": configured["server_id"], "role_id": "1007"}, headers=H)
        found = (await client.post(f"{A}/events/{event_id}/signup-roles/find", json={"server_id": configured["server_id"]}, headers=H)).json()["roles"]
        by_id = {r["id"]: r for r in found}
        assert set(by_id) == {"1004", "1005"} and by_id["1004"]["unsafe_reason"] is None and "Manage Messages" in by_id["1005"]["unsafe_reason"]

    async def test_a_found_role_can_be_linked(self, client, sf, configured, discord):
        discord.add_role(GUILD, "1008", "Bear Hunt #9")
        event_id = await an_event(sf, configured, name="Bear Hunt #9")
        response = await client.put(f"{A}/events/{event_id}/signup-roles", json={"server_id": configured["server_id"], "role_id": "1008"}, headers=H)
        assert response.status_code == 200 and response.json()["created_by_samaya"] is False

    async def test_a_relinked_role_keeps_its_subscribers(self, client, sf, configured, discord):
        event_id = await an_event(sf, configured)
        await client.put(f"{A}/events/{event_id}/signup-roles", json={"server_id": configured["server_id"], "role_id": "1001"}, headers=H)
        async with sf() as s:
            s.add(EventSubscription(server_id=configured["server_id"], role_id="1001", voter_hash="h" * 64))
            await s.commit()
        async with sf() as s:  # the mapping row is lost (an event recreated), the subscriptions are not
            await s.delete((await s.execute(select(SignupRole))).scalar_one())
            await s.commit()
        await client.put(f"{A}/events/{event_id}/signup-roles", json={"server_id": configured["server_id"], "role_id": "1001"}, headers=H)
        body = (await client.get(f"{A}/events/{event_id}", headers=H)).json()
        assert [r["subscribers"] for r in body["signup_roles"]] == [1]

    async def test_sync_name_renames_a_role_samaya_created(self, client, sf, configured, discord):
        event_id = await an_event(sf, configured, name="Old Name")
        made = (await client.put(f"{A}/events/{event_id}/signup-roles", json={"server_id": configured["server_id"], "create": True}, headers=H)).json()
        await client.patch(f"{A}/events/{event_id}", json={"name": "New Name"}, headers=H)
        response = await client.post(f"{A}/events/{event_id}/signup-roles/sync-name", json={"server_id": configured["server_id"]}, headers=H)
        assert response.status_code == 200 and response.json()["role_name"] == "New Name"
        assert discord.guild_roles[GUILD][made["role_id"]]["name"] == "New Name"

    async def test_renaming_the_event_alone_never_renames_the_role(self, client, sf, configured, discord):
        event_id = await an_event(sf, configured, name="Old Name")
        made = (await client.put(f"{A}/events/{event_id}/signup-roles", json={"server_id": configured["server_id"], "create": True}, headers=H)).json()
        await client.patch(f"{A}/events/{event_id}", json={"name": "New Name"}, headers=H)
        assert discord.guild_roles[GUILD][made["role_id"]]["name"] == "Old Name"
        assert not [c for c in discord.calls if c[0] == "rename_role"]

    async def test_sync_name_refuses_a_role_it_did_not_create(self, client, sf, configured, discord):
        event_id = await an_event(sf, configured)
        await client.put(f"{A}/events/{event_id}/signup-roles", json={"server_id": configured["server_id"], "role_id": "1001"}, headers=H)
        response = await client.post(f"{A}/events/{event_id}/signup-roles/sync-name", json={"server_id": configured["server_id"]}, headers=H)
        assert response.status_code == 422 and not [c for c in discord.calls if c[0] == "rename_role"]

    async def test_sync_name_without_a_mapping_is_404(self, client, sf, configured, discord):
        event_id = await an_event(sf, configured)
        response = await client.post(f"{A}/events/{event_id}/signup-roles/sync-name", json={"server_id": configured["server_id"]}, headers=H)
        assert response.status_code == 404


# ── Event body, split, delete, headcount ─────────────────────

class TestEventBody:
    async def test_roles_counts_and_warnings(self, client, sf, configured, discord):
        event_id = await an_event(sf, configured)
        body = (await client.get(f"{A}/events/{event_id}", headers=H)).json()
        assert body["signup_warnings"] and "no Notify me role" in body["signup_warnings"][0]
        assert body["signup_roles"][0]["role_id"] is None
        await client.put(f"{A}/events/{event_id}/signup-roles", json={"server_id": configured["server_id"], "role_id": "1001"}, headers=H)
        async with sf() as s:
            s.add_all([EventSubscription(server_id=configured["server_id"], role_id="1001", voter_hash=c * 64) for c in "ab"])
            await s.commit()
        body = (await client.get(f"{A}/events/{event_id}", headers=H)).json()
        assert body["signup_warnings"] == []
        assert [(r["scope"], r["role_name"], r["subscribers"]) for r in body["signup_roles"]] == [("event", "Bear Hunt", 2)]

    async def test_no_warning_when_the_button_is_off(self, client, sf, configured, discord):
        event_id = await an_event(sf, configured)
        body = (await client.patch(f"{A}/events/{event_id}", json={"signup_enabled": False}, headers=H)).json()
        assert body["signup_warnings"] == []

    async def test_two_group_events_sharing_the_types_role_are_warned(self, client, sf, configured, discord):
        gid = (await group(client)).json()["id"]
        first, second = await an_event(sf, configured, name="One"), await an_event(sf, configured, name="Two")
        for event_id in (first, second):
            await client.patch(f"{A}/events/{event_id}", json={"signup_group_id": gid}, headers=H)
        async with sf() as s:
            type_id = (await s.get(Event, first)).type_id
        await client.put(f"{A}/event-types/{type_id}/signup-roles", json={"server_id": configured["server_id"], "role_id": "1001"}, headers=H)
        body = (await client.get(f"{A}/events/{first}", headers=H)).json()
        assert any("Two uses the same role" in w for w in body["signup_warnings"])

    async def test_no_player_identity_is_in_any_payload(self, client, sf, configured, discord):
        event_id = await an_event(sf, configured)
        await client.put(f"{A}/events/{event_id}/signup-roles", json={"server_id": configured["server_id"], "role_id": "1001"}, headers=H)
        digest = sub_hash(configured["server_id"], "1001", USER)
        async with sf() as s:
            s.add(EventSubscription(server_id=configured["server_id"], role_id="1001", voter_hash=digest))
            s.add(OccurrenceRsvp(event_id=event_id, occurrence_date=SOON, voter_hash="r" * 64))
            await s.commit()
        for url in (f"{A}/events/{event_id}", f"{A}/events", f"{A}/event-types", f"{A}/occurrences?from={TODAY}&to={TODAY + timedelta(days=30)}", f"{A}/signup-groups"):
            text = (await client.get(url, headers=H)).text
            assert digest not in text and "r" * 64 not in text and USER not in text

    async def test_the_occurrence_list_carries_a_count(self, client, sf, configured, discord):
        event_id = await an_event(sf, configured)
        async with sf() as s:
            s.add_all([OccurrenceRsvp(event_id=event_id, occurrence_date=SOON, voter_hash=c * 64) for c in "abc"])
            await s.commit()
        rows = (await client.get(f"{A}/occurrences?from={TODAY}&to={TODAY + timedelta(days=30)}", headers=H)).json()
        assert [(r["occurrence_date"], r["rsvp_count"], r["rsvp_enabled"]) for r in rows] == [(str(SOON), 3, True)]


class TestSplitAndDelete:
    async def _recurring(self, client, sf, configured, discord):
        event_id = await an_event(sf, configured, interval=1, anchor=TODAY + timedelta(days=1))
        await client.put(f"{A}/events/{event_id}/signup-roles", json={"server_id": configured["server_id"], "role_id": "1001"}, headers=H)
        return event_id

    async def test_a_split_copies_the_role_and_keeps_the_subscribers(self, client, sf, configured, discord):
        event_id = await self._recurring(client, sf, configured, discord)
        async with sf() as s:
            s.add(EventSubscription(server_id=configured["server_id"], role_id="1001", voter_hash="h" * 64))
            await s.commit()
        response = await client.post(f"{A}/events/{event_id}/split", json={"from_date": str(TODAY + timedelta(days=4)), "changes": {}}, headers=H)
        assert response.status_code == 201, response.text
        new_id = response.json()["created"]["id"]
        created = (await client.get(f"{A}/events/{new_id}", headers=H)).json()
        assert [(r["role_id"], r["scope"], r["subscribers"]) for r in created["signup_roles"]] == [("1001", "event", 1)]
        original = (await client.get(f"{A}/events/{event_id}", headers=H)).json()
        assert [r["role_id"] for r in original["signup_roles"]] == ["1001"]
        assert created["signup_enabled"] is True and created["rsvp_enabled"] is True

    async def test_a_split_moves_later_rsvps_and_copies_the_group(self, client, sf, configured, discord):
        event_id = await self._recurring(client, sf, configured, discord)
        gid = (await group(client)).json()["id"]
        await client.patch(f"{A}/events/{event_id}", json={"signup_group_id": gid}, headers=H)
        cut = TODAY + timedelta(days=4)
        async with sf() as s:
            s.add_all([OccurrenceRsvp(event_id=event_id, occurrence_date=cut - timedelta(days=1), voter_hash="a" * 64),
                       OccurrenceRsvp(event_id=event_id, occurrence_date=cut, voter_hash="b" * 64),
                       OccurrenceRsvp(event_id=event_id, occurrence_date=cut + timedelta(days=1), voter_hash="c" * 64)])
            await s.commit()
        new_id = (await client.post(f"{A}/events/{event_id}/split", json={"from_date": str(cut), "changes": {}}, headers=H)).json()["created"]["id"]
        async with sf() as s:
            rows = {(r.event_id, r.occurrence_date) for r in (await s.execute(select(OccurrenceRsvp))).scalars()}
            assert rows == {(event_id, cut - timedelta(days=1)), (new_id, cut), (new_id, cut + timedelta(days=1))}
            assert (await s.get(Event, new_id)).signup_group_id == gid

    async def test_deleting_an_event_removes_its_roles_subscribers_and_rsvps(self, client, sf, configured, discord):
        event_id = await self._recurring(client, sf, configured, discord)
        async with sf() as s:
            s.add(EventSubscription(server_id=configured["server_id"], role_id="1001", voter_hash="h" * 64))
            s.add(OccurrenceRsvp(event_id=event_id, occurrence_date=SOON, voter_hash="r" * 64))
            await s.commit()
        assert (await client.delete(f"{A}/events/{event_id}", headers=H)).status_code == 204
        async with sf() as s:
            for model in (SignupRole, EventSubscription, OccurrenceRsvp):
                assert (await s.execute(select(model))).first() is None

    async def test_deleting_an_event_keeps_a_role_another_event_still_uses(self, client, sf, configured, discord):
        first, second = await an_event(sf, configured, name="One"), await an_event(sf, configured, name="Two")
        for event_id in (first, second):
            await client.put(f"{A}/events/{event_id}/signup-roles", json={"server_id": configured["server_id"], "role_id": "1001"}, headers=H)
        async with sf() as s:
            s.add(EventSubscription(server_id=configured["server_id"], role_id="1001", voter_hash="h" * 64))
            await s.commit()
        await client.delete(f"{A}/events/{first}", headers=H)
        async with sf() as s:
            assert len((await s.execute(select(EventSubscription))).scalars().all()) == 1


class TestNothingIsPublic:
    async def test_the_public_payloads_carry_nothing_about_signup(self, client_no_session, sf, configured):
        event_id = await an_event(sf, configured)
        async with sf() as s:
            s.add(SignupRole(event_id=event_id, server_id=configured["server_id"], role_id="role-secret", role_name="Hidden"))
            s.add(OccurrenceRsvp(event_id=event_id, occurrence_date=SOON, voter_hash="r" * 64))
            await s.commit()
        for url in ("/api/events", f"/api/events/{configured['slug']}", "/events.ics", f"/events/{configured['slug']}.ics"):
            text = (await client_no_session.get(url)).text.lower()
            for word in ("rsvp", "signup", "subscri", "role-secret", "hidden", "going"):
                assert word not in text, (url, word)


# ── The signed webhook route ─────────────────────────────────

@pytest.fixture
def post(monkeypatch, client_no_session, sf, discord):
    from main import app
    key = SigningKey.generate()
    monkeypatch.setattr(webhooks, "PLATFORM_PUBLIC_KEY", key.verify_key.encode().hex())
    app.dependency_overrides[get_session_factory] = lambda: sf

    async def send(payload):
        body = json.dumps(payload).encode()
        timestamp = "1700000000"
        signature = key.sign(timestamp.encode() + body).signature.hex()
        return await client_no_session.post("/webhooks/discord", content=body, headers={
            "X-Signature-Ed25519": signature, "X-Signature-Timestamp": timestamp, "Content-Type": "application/json"})
    yield send
    app.dependency_overrides.pop(get_session_factory, None)


def _interaction(custom_id, roles=()):
    return {"type": 3, "guild_id": GUILD, "channel_id": "chan-mod", "application_id": "app-9", "token": "tok-9",
            "member": {"user": {"id": USER}, "roles": list(roles)}, "data": {"component_type": 2, "custom_id": custom_id}}


class TestWebhookRoute:
    async def test_notify_me_answers_deferred_then_changes_the_role_and_edits_the_reply(self, post, sf, configured, discord):
        event_id = await an_event(sf, configured)
        async with sf() as s:
            s.add(SignupRole(event_id=event_id, server_id=configured["server_id"], role_id="1001", role_name="Bear Hunt"))
            await s.commit()
        response = await post(_interaction(sub_custom_id(event_id)))
        assert response.status_code == 200 and response.json() == {"type": 5, "data": {"flags": 64}}
        assert ("add_role", GUILD, USER, "1001") in discord.calls
        assert discord.interaction_edits == [("app-9", "tok-9", "You will now be notified about Bear Hunt.")]
        async with sf() as s:
            assert len((await s.execute(select(EventSubscription))).scalars().all()) == 1

    async def test_im_in_answers_immediately_with_the_count(self, post, sf, configured, discord):
        from services.event_engine import run_delivery_tick
        event_id = await an_event(sf, configured, anchor=TODAY + timedelta(days=1), reminders=(1440,))
        await run_delivery_tick(sf, discord, datetime.now(UTC) + timedelta(hours=1))
        response = await post(_interaction(rsvp_custom_id(event_id, TODAY + timedelta(days=1))))
        data = response.json()["data"]
        assert response.json()["type"] == 4 and data["flags"] == 64 and "1 going." in data["content"]
        assert data["allowed_mentions"] == {"parse": []}
        assert discord.interaction_edits == []

    async def test_poll_buttons_still_reach_the_poll_handler(self, post, sf, configured, discord):
        response = await post(_interaction("tp:1:1"))
        assert response.json()["data"]["content"] == "This poll is closed."

    async def test_an_unknown_button_is_a_closed_poll_not_an_error(self, post, sf, configured, discord):
        response = await post(_interaction("zz:1"))
        assert response.status_code == 200 and response.json()["data"]["flags"] == 64

    async def test_a_bad_signature_still_fails_before_anything_happens(self, client_no_session, monkeypatch, discord):
        monkeypatch.setattr(webhooks, "PLATFORM_PUBLIC_KEY", SigningKey.generate().verify_key.encode().hex())
        response = await client_no_session.post("/webhooks/discord", json=_interaction("sub:1"),
                                                 headers={"X-Signature-Ed25519": "00" * 64, "X-Signature-Timestamp": "1"})
        assert response.status_code == 401 and discord.calls == []
