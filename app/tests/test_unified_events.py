"""Unified event model, Phase 1 (spec §66): event types and event
definitions through the admin API at /api.

The default `client` is a superadmin on purpose (see conftest.py); the
permission tests use make_user_and_client for a deliberately unprivileged
user instead.
"""
import pytest
from sqlalchemy import select

from models.db import AuditLog, Event, EventAlliance, EventReminder, EventType

BASE = "/admin/api"


async def _make_type(client, **overrides):
    body = {"name": "Major", "color": "#E65100", "default_duration_hours": 2, "default_interval_days": 7,
            "default_message": "{event_time_relative}", "default_reminder_minutes": [60, 10],
            "default_mention_role": True}
    body.update(overrides)
    resp = await client.post(f"{BASE}/event-types", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _event_body(type_id, **overrides):
    body = {"type_id": type_id, "name": "Bear Hunt", "start_time_utc": "19:00", "anchor_date": "2026-10-05"}
    body.update(overrides)
    return body


class TestEventTypes:
    async def test_create_and_list(self, client):
        created = await _make_type(client)
        assert created["color"] == "#E65100"
        assert created["default_reminder_minutes"] == [60, 10]
        listed = (await client.get(f"{BASE}/event-types")).json()
        assert [t["name"] for t in listed] == ["Major"]

    async def test_duplicate_name_is_a_friendly_422(self, client):
        await _make_type(client)
        resp = await client.post(f"{BASE}/event-types", json={"name": "Major"})
        assert resp.status_code == 422
        assert "already exists" in resp.json()["detail"]

    @pytest.mark.parametrize("body", [
        {"name": "Bad", "color": "orange"},
        {"name": "Bad", "default_duration_hours": 0},
        {"name": "Bad", "default_interval_days": 0},
        {"name": "Bad", "default_reminder_minutes": [-5]},
        {"name": "Bad", "default_reminder_minutes": [10, 10]},
        {"name": "   "},
    ])
    async def test_validation(self, client, body):
        resp = await client.post(f"{BASE}/event-types", json=body)
        assert resp.status_code == 422

    async def test_patch_can_clear_duration(self, client):
        created = await _make_type(client)
        resp = await client.patch(f"{BASE}/event-types/{created['id']}", json={"default_duration_hours": None, "name": "Big"})
        assert resp.status_code == 200
        assert resp.json()["default_duration_hours"] is None
        assert resp.json()["name"] == "Big"

    async def test_delete_blocked_while_in_use(self, client):
        etype = await _make_type(client)
        assert (await client.post(f"{BASE}/events", json=_event_body(etype["id"]))).status_code == 201
        resp = await client.delete(f"{BASE}/event-types/{etype['id']}")
        assert resp.status_code == 409

    async def test_delete_unused(self, client, db_session):
        etype = await _make_type(client)
        assert (await client.delete(f"{BASE}/event-types/{etype['id']}")).status_code == 204
        assert (await db_session.execute(select(EventType).where(EventType.id == etype["id"]))).first() is None

    async def test_new_kingdom_gets_a_general_type(self, client, db_session):
        resp = await client.post("/admin/api/kingdoms", json={"name": "Kingdom 7", "slug": "k7"})
        assert resp.status_code in (200, 201), resp.text
        rows = (await db_session.execute(select(EventType).where(EventType.kingdom_id == resp.json()["id"]))).scalars().all()
        assert [t.name for t in rows] == ["General"]

    async def test_types_are_per_kingdom(self, client, db_session, tenant):
        from models.db import Kingdom
        other = Kingdom(name="Other", slug="other")
        db_session.add(other)
        await db_session.flush()
        foreign = EventType(kingdom_id=other.id, name="Foreign")
        db_session.add(foreign)
        await db_session.commit()
        assert [t["name"] for t in (await client.get(f"{BASE}/event-types")).json()] == []
        assert (await client.patch(f"{BASE}/event-types/{foreign.id}", json={"name": "x"})).status_code == 404


class TestCreateEvent:
    async def test_defaults_come_from_the_type(self, client):
        etype = await _make_type(client)
        resp = await client.post(f"{BASE}/events", json=_event_body(etype["id"]))
        assert resp.status_code == 201, resp.text
        e = resp.json()
        assert e["duration_hours"] == 2.0 and e["has_calendar_entry"] is True
        assert e["recurrence_kind"] == "interval_days" and e["interval_days"] == 7
        assert e["message"] == "{event_time_relative}"
        assert e["mention_role"] is True
        assert e["reminder_minutes"] == [60, 10]
        assert e["type"]["name"] == "Major"

    async def test_explicit_values_beat_type_defaults(self, client):
        etype = await _make_type(client)
        resp = await client.post(f"{BASE}/events", json=_event_body(
            etype["id"], duration_hours=None, recurrence_kind="none", message="Hello", mention_role=False, reminder_minutes=[5]))
        e = resp.json()
        assert e["duration_hours"] is None and e["has_calendar_entry"] is False
        assert e["recurrence_kind"] == "none" and e["interval_days"] is None
        assert e["message"] == "Hello" and e["mention_role"] is False and e["reminder_minutes"] == [5]

    async def test_owner_is_always_in_an_alliance_events_audience(self, client, tenant):
        etype = await _make_type(client)
        e = (await client.post(f"{BASE}/events", json=_event_body(etype["id"]))).json()
        assert [a["tenant_id"] for a in e["alliances"]] == [tenant["id"]]

    async def test_audience_subset_with_override(self, client, tenant, second_tenant):
        etype = await _make_type(client)
        resp = await client.post(f"{BASE}/events", json=_event_body(
            etype["id"], alliances=[{"tenant_slug": "nsr", "message_override": "NSR wording"}]))
        assert resp.status_code == 201, resp.text
        by_tenant = {a["tenant_id"]: a for a in resp.json()["alliances"]}
        assert set(by_tenant) == {tenant["id"], second_tenant["id"]}
        assert by_tenant[second_tenant["id"]]["message_override"] == "NSR wording"
        assert by_tenant[tenant["id"]]["message_override"] is None

    async def test_kingdom_wide_needs_no_audience_rows(self, client):
        etype = await _make_type(client)
        e = (await client.post(f"{BASE}/events", json=_event_body(etype["id"], scope="kingdom-wide"))).json()
        assert e["scope"] == "kingdom-wide" and e["alliances"] == []

    async def test_alliance_from_another_kingdom_is_rejected(self, client, db_session):
        from models.db import DiscordServer, Kingdom, Tenant
        k2 = Kingdom(name="K2", slug="k2")
        db_session.add(k2)
        await db_session.flush()
        srv = DiscordServer(kingdom_id=k2.id, name="s", guild_id="g-other")
        db_session.add(srv)
        await db_session.flush()
        db_session.add(Tenant(kingdom_id=k2.id, server_id=srv.id, name="Far", slug="far"))
        await db_session.commit()
        etype = await _make_type(client)
        resp = await client.post(f"{BASE}/events", json=_event_body(etype["id"], alliances=[{"tenant_slug": "far"}]))
        assert resp.status_code == 422

    async def test_unknown_alliance_slug_404(self, client):
        etype = await _make_type(client)
        resp = await client.post(f"{BASE}/events", json=_event_body(etype["id"], alliances=[{"tenant_slug": "nope"}]))
        assert resp.status_code == 404

    async def test_duplicate_alliance_rows_rejected(self, client, second_tenant):
        etype = await _make_type(client)
        resp = await client.post(f"{BASE}/events", json=_event_body(
            etype["id"], alliances=[{"tenant_slug": "nsr"}, {"tenant_slug": "nsr"}]))
        assert resp.status_code == 422

    @pytest.mark.parametrize("overrides", [
        {"recurrence_kind": "none", "interval_days": 3},
        {"recurrence_kind": "interval_days", "interval_days": None},
        {"until_date": "2026-10-01"},
        {"start_time_utc": "25:00"},
        {"anchor_date": "not-a-date"},
        {"scope": "galaxy"},
        {"duration_hours": -1},
        {"reminder_minutes": [-1]},
        {"message": "x" * 2001},
        {"recurrence_kind": "monthly"},
    ])
    async def test_validation(self, client, overrides):
        etype = await _make_type(client)
        resp = await client.post(f"{BASE}/events", json=_event_body(etype["id"], **overrides))
        assert resp.status_code == 422, resp.text

    async def test_type_from_another_kingdom_404(self, client, db_session):
        from models.db import Kingdom
        other = Kingdom(name="Other", slug="other")
        db_session.add(other)
        await db_session.flush()
        foreign = EventType(kingdom_id=other.id, name="Foreign")
        db_session.add(foreign)
        await db_session.commit()
        assert (await client.post(f"{BASE}/events", json=_event_body(foreign.id))).status_code == 404

    async def test_create_writes_an_audit_row(self, client, db_session):
        etype = await _make_type(client)
        e = (await client.post(f"{BASE}/events", json=_event_body(etype["id"]))).json()
        rows = (await db_session.execute(
            select(AuditLog).where(AuditLog.table_name == "events", AuditLog.row_id == e["id"]))).scalars().all()
        assert [r.action for r in rows] == ["create"]


class TestUpdateAndDelete:
    async def _event(self, client, **overrides):
        etype = await _make_type(client)
        return etype, (await client.post(f"{BASE}/events", json=_event_body(etype["id"], **overrides))).json()

    async def test_patch_scalar_fields(self, client):
        _, e = await self._event(client)
        resp = await client.patch(f"{BASE}/events/{e['id']}", json={"name": "Renamed", "leadership_only": True, "location": "Rally hall"})
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert (out["name"], out["leadership_only"], out["location"]) == ("Renamed", True, "Rally hall")

    async def test_patch_to_one_off_clears_interval(self, client):
        _, e = await self._event(client)
        out = (await client.patch(f"{BASE}/events/{e['id']}", json={"recurrence_kind": "none"})).json()
        assert out["recurrence_kind"] == "none" and out["interval_days"] is None

    async def test_patch_interval_alone_makes_it_repeat(self, client):
        _, e = await self._event(client, recurrence_kind="none", interval_days=None)
        out = (await client.patch(f"{BASE}/events/{e['id']}", json={"interval_days": 14})).json()
        assert out["recurrence_kind"] == "interval_days" and out["interval_days"] == 14

    async def test_patch_that_breaks_recurrence_shape_is_422(self, client):
        _, e = await self._event(client)
        resp = await client.patch(f"{BASE}/events/{e['id']}", json={"until_date": "2020-01-01"})
        assert resp.status_code == 422

    async def test_patch_can_remove_duration_to_make_a_message(self, client):
        _, e = await self._event(client)
        out = (await client.patch(f"{BASE}/events/{e['id']}", json={"duration_hours": None})).json()
        assert out["duration_hours"] is None and out["has_calendar_entry"] is False

    async def test_patch_reminders_keeping_one_value(self, client, db_session):
        _, e = await self._event(client)  # [60, 10]
        out = (await client.patch(f"{BASE}/events/{e['id']}", json={"reminder_minutes": [10, 5]})).json()
        assert out["reminder_minutes"] == [10, 5]
        rows = (await db_session.execute(select(EventReminder).where(EventReminder.event_id == e["id"]))).scalars().all()
        assert sorted(r.minutes_before for r in rows) == [5, 10]

    async def test_patch_alliances_updates_override_in_place(self, client, second_tenant, db_session):
        _, e = await self._event(client, alliances=[{"tenant_slug": "nsr", "message_override": "one"}])
        resp = await client.patch(f"{BASE}/events/{e['id']}", json={"alliances": [{"tenant_slug": "nsr", "message_override": "two"}]})
        assert resp.status_code == 200, resp.text
        by_tenant = {a["tenant_id"]: a for a in resp.json()["alliances"]}
        assert by_tenant[second_tenant["id"]]["message_override"] == "two"
        count = len((await db_session.execute(select(EventAlliance).where(EventAlliance.event_id == e["id"]))).scalars().all())
        assert count == 2  # owner is re-added automatically

    async def test_patch_scope_back_to_alliance_leaves_owner_only(self, client, tenant):
        _, e = await self._event(client, scope="kingdom-wide")
        out = (await client.patch(f"{BASE}/events/{e['id']}", json={"scope": "alliance"})).json()
        assert [a["tenant_id"] for a in out["alliances"]] == [tenant["id"]]

    async def test_deactivate_via_patch(self, client):
        _, e = await self._event(client)
        assert (await client.patch(f"{BASE}/events/{e['id']}", json={"active": False})).json()["active"] is False

    async def test_patch_cover_image_empty_string_clears_and_null_keeps(self, client):
        png = "data:image/png;base64,iVBORw0KGgo="
        _, e = await self._event(client, cover_image_data=png)
        assert e["cover_image_data"] == png
        assert (await client.patch(f"{BASE}/events/{e['id']}", json={"cover_image_data": None})).json()["cover_image_data"] == png
        assert (await client.patch(f"{BASE}/events/{e['id']}", json={"cover_image_data": ""})).json()["cover_image_data"] is None

    async def test_patch_unknown_event_404(self, client):
        assert (await client.patch(f"{BASE}/events/999", json={"name": "x"})).status_code == 404

    async def test_delete_cascades_children_and_audits(self, client, db_session):
        _, e = await self._event(client)
        assert (await client.delete(f"{BASE}/events/{e['id']}")).status_code == 204
        assert (await db_session.execute(select(Event).where(Event.id == e["id"]))).first() is None
        assert (await db_session.execute(select(EventReminder).where(EventReminder.event_id == e["id"]))).first() is None
        actions = [r.action for r in (await db_session.execute(
            select(AuditLog).where(AuditLog.table_name == "events", AuditLog.row_id == e["id"]).order_by(AuditLog.id))).scalars()]
        assert actions == ["create", "delete"]

    async def test_update_audit_records_before_and_after(self, client, db_session):
        import json
        _, e = await self._event(client)
        await client.patch(f"{BASE}/events/{e['id']}", json={"name": "Renamed"})
        row = (await db_session.execute(
            select(AuditLog).where(AuditLog.table_name == "events", AuditLog.action == "update"))).scalar_one()
        assert json.loads(row.before)["name"] == "Bear Hunt" and json.loads(row.after)["name"] == "Renamed"


class TestListAndVisibility:
    async def test_filters(self, client):
        major = await _make_type(client)
        minor = await _make_type(client, name="Minor")
        await client.post(f"{BASE}/events", json=_event_body(major["id"], name="A"))
        await client.post(f"{BASE}/events", json=_event_body(minor["id"], name="B", scope="kingdom-wide"))
        by_type = (await client.get(f"{BASE}/events", params={"type_id": minor["id"]})).json()
        assert [e["name"] for e in by_type] == ["B"]
        by_scope = (await client.get(f"{BASE}/events", params={"scope": "alliance"})).json()
        assert [e["name"] for e in by_scope] == ["A"]

    async def test_other_alliance_sees_audience_and_kingdom_wide_events_only(self, client, tenant, second_tenant):
        etype = await _make_type(client)
        await client.post(f"{BASE}/events", json=_event_body(etype["id"], name="Private"))
        await client.post(f"{BASE}/events", json=_event_body(etype["id"], name="Shared", alliances=[{"tenant_slug": "nsr"}]))
        await client.post(f"{BASE}/events", json=_event_body(etype["id"], name="Everyone", scope="kingdom-wide"))
        client.headers["X-Tenant-Slug"] = "nsr"
        names = sorted(e["name"] for e in (await client.get(f"{BASE}/events")).json())
        assert names == ["Everyone", "Shared"]

    async def test_combined_view_has_no_duplicates(self, client, second_tenant):
        etype = await _make_type(client)
        await client.post(f"{BASE}/events", json=_event_body(etype["id"], name="Shared", alliances=[{"tenant_slug": "nsr"}]))
        client.headers["X-Tenant-Slug"] = "*"
        assert [e["name"] for e in (await client.get(f"{BASE}/events")).json()] == ["Shared"]

    async def test_get_one(self, client, second_tenant):
        etype = await _make_type(client)
        e = (await client.post(f"{BASE}/events", json=_event_body(etype["id"], name="Private"))).json()
        assert (await client.get(f"{BASE}/events/{e['id']}")).status_code == 200
        client.headers["X-Tenant-Slug"] = "nsr"
        assert (await client.get(f"{BASE}/events/{e['id']}")).status_code == 404


class TestPermissions:
    async def test_viewer_cannot_write(self, make_user_and_client, tenant):
        viewer, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")])
        viewer.headers["X-Tenant-Slug"] = tenant["slug"]
        assert (await viewer.get(f"{BASE}/event-types")).status_code == 200
        assert (await viewer.post(f"{BASE}/events", json=_event_body(1))).status_code == 403
        assert (await viewer.post(f"{BASE}/event-types", json={"name": "x"})).status_code == 403

    async def test_coordinator_creates_events_but_not_types_or_kingdom_wide(self, client, make_user_and_client, tenant):
        etype = await _make_type(client)
        coord, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "coordinator")])
        coord.headers["X-Tenant-Slug"] = tenant["slug"]
        assert (await coord.post(f"{BASE}/events", json=_event_body(etype["id"]))).status_code == 201
        assert (await coord.post(f"{BASE}/events", json=_event_body(etype["id"], scope="kingdom-wide"))).status_code == 403
        assert (await coord.post(f"{BASE}/event-types", json={"name": "Mine"})).status_code == 403

    async def test_kingdom_coordinator_can_manage_types_and_kingdom_wide(self, client, make_user_and_client, tenant):
        etype = await _make_type(client)
        kc, _ = await make_user_and_client(
            tenant_grants=[(tenant["id"], "coordinator")], kingdom_grants=[tenant["kingdom_id"]], discord_id="kc")
        kc.headers["X-Tenant-Slug"] = tenant["slug"]
        assert (await kc.post(f"{BASE}/event-types", json={"name": "Mine"})).status_code == 201
        assert (await kc.post(f"{BASE}/events", json=_event_body(etype["id"], scope="kingdom-wide"))).status_code == 201

    async def test_coordinator_cannot_edit_another_alliances_event(self, client, make_user_and_client, tenant, second_tenant):
        etype = await _make_type(client)
        e = (await client.post(f"{BASE}/events", json=_event_body(etype["id"]))).json()
        other, _ = await make_user_and_client(tenant_grants=[(second_tenant["id"], "coordinator")], discord_id="o")
        other.headers["X-Tenant-Slug"] = second_tenant["slug"]
        assert (await other.patch(f"{BASE}/events/{e['id']}", json={"name": "Hijack"})).status_code == 404
        assert (await other.delete(f"{BASE}/events/{e['id']}")).status_code == 404

    async def test_alliance_event_audience_needs_access_to_each_listed_alliance(
        self, client, make_user_and_client, tenant, second_tenant,
    ):
        etype = await _make_type(client)
        coord, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "coordinator")], discord_id="c2")
        coord.headers["X-Tenant-Slug"] = tenant["slug"]
        resp = await coord.post(f"{BASE}/events", json=_event_body(etype["id"], alliances=[{"tenant_slug": "nsr"}]))
        assert resp.status_code == 403

    async def test_unauthenticated(self, client_no_session):
        assert (await client_no_session.get(f"{BASE}/events")).status_code in (400, 401)
