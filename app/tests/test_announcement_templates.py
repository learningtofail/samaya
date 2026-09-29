"""Tests for spec §27 — Announcement Templates and the placeholder
resolution they exist to support. Covers the /admin/api/announcement-templates
CRUD surface, tenant scoping, and — the actual point of the feature — that
{alliance_name}/{kingdom_name}/{event_time}/etc. in an announcement's
body_markdown get resolved per-target at delivery time, not once at
creation, so a recurring announcement's later occurrences get a fresh
Discord timestamp and each target sees its own alliance name.
"""
from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models.db import Announcement, AnnouncementTemplate
from scheduler.announcements import send_scheduled_announcements


def _future_iso(minutes=60):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


async def _backdate_to_due(db_session, announcement_id):
    announcement = await db_session.get(Announcement, announcement_id)
    announcement.scheduled_for = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db_session.commit()


async def _run_delivery_job(db_engine):
    TestSessionLocal = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    await send_scheduled_announcements(session_factory=TestSessionLocal)


class TestAnnouncementTemplateCrud:

    async def test_create_and_list(self, client: AsyncClient, tenant: dict):
        r = await client.post("/admin/api/announcement-templates", json={
            "name": "Daily Reset Warning",
            "title_template": "Daily Reset Warning",
            "body_template": "⚠️ Reset {event_time_relative}",
            "event_offset_minutes": 15,
        })
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["name"] == "Daily Reset Warning"
        assert body["event_offset_minutes"] == 15
        assert body["leadership_only"] is False

        r = await client.get("/admin/api/announcement-templates")
        assert r.status_code == 200
        assert len(r.json()) == 1

    async def test_duplicate_name_same_tenant_rejected(self, client: AsyncClient, tenant: dict):
        payload = {"name": "Dup", "title_template": "t", "body_template": "b"}
        r1 = await client.post("/admin/api/announcement-templates", json=payload)
        assert r1.status_code == 201
        r2 = await client.post("/admin/api/announcement-templates", json=payload)
        assert r2.status_code == 422

    async def test_update_template(self, client: AsyncClient, tenant: dict):
        created = await client.post("/admin/api/announcement-templates", json={
            "name": "T", "title_template": "Title", "body_template": "Body",
        })
        template_id = created.json()["id"]

        r = await client.patch(f"/admin/api/announcement-templates/{template_id}", json={
            "body_template": "New body {alliance_name}", "event_offset_minutes": 30,
        })
        assert r.status_code == 200, r.text
        assert r.json()["body_template"] == "New body {alliance_name}"
        assert r.json()["event_offset_minutes"] == 30
        assert r.json()["title_template"] == "Title"  # untouched field preserved

    async def test_delete_template(self, client: AsyncClient, tenant: dict):
        created = await client.post("/admin/api/announcement-templates", json={
            "name": "ToDelete", "title_template": "t", "body_template": "b",
        })
        template_id = created.json()["id"]
        r = await client.delete(f"/admin/api/announcement-templates/{template_id}")
        assert r.status_code == 200
        assert (await client.get("/admin/api/announcement-templates")).json() == []

    async def test_update_nonexistent_404s(self, client: AsyncClient, tenant: dict):
        r = await client.patch("/admin/api/announcement-templates/999999", json={"name": "x"})
        assert r.status_code == 404

    async def test_delete_nonexistent_404s(self, client: AsyncClient, tenant: dict):
        r = await client.delete("/admin/api/announcement-templates/999999")
        assert r.status_code == 404


class TestAnnouncementTemplateTenantScoping:

    async def test_templates_scoped_to_owning_tenant(
        self, client: AsyncClient, tenant: dict, second_tenant: dict
    ):
        await client.post(
            "/admin/api/announcement-templates",
            json={"name": "MOD-only", "title_template": "t", "body_template": "b"},
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        r = await client.get("/admin/api/announcement-templates", headers={"X-Tenant-Slug": second_tenant["slug"]})
        assert r.json() == []

    async def test_same_name_allowed_across_different_tenants(
        self, client: AsyncClient, tenant: dict, second_tenant: dict
    ):
        payload = {"name": "Shared Name", "title_template": "t", "body_template": "b"}
        r1 = await client.post("/admin/api/announcement-templates", json=payload, headers={"X-Tenant-Slug": tenant["slug"]})
        r2 = await client.post("/admin/api/announcement-templates", json=payload, headers={"X-Tenant-Slug": second_tenant["slug"]})
        assert r1.status_code == 201
        assert r2.status_code == 201

    async def test_cannot_update_another_tenants_template(
        self, client: AsyncClient, tenant: dict, second_tenant: dict
    ):
        created = await client.post(
            "/admin/api/announcement-templates",
            json={"name": "T", "title_template": "t", "body_template": "b"},
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        template_id = created.json()["id"]
        r = await client.patch(
            f"/admin/api/announcement-templates/{template_id}", json={"name": "Hijacked"},
            headers={"X-Tenant-Slug": second_tenant["slug"]},
        )
        assert r.status_code == 404


class TestAnnouncementCreatedWithEventOffset:

    async def test_event_offset_minutes_round_trips(self, client: AsyncClient, tenant: dict):
        r = await client.post("/admin/api/announcements", json={
            "title": "Reset Warning", "body_markdown": "Reset {event_time_relative}",
            "scheduled_for": _future_iso(), "event_offset_minutes": 15,
            "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
        })
        assert r.status_code == 201, r.text
        assert r.json()["event_offset_minutes"] == 15

    async def test_defaults_to_zero(self, client: AsyncClient, tenant: dict):
        r = await client.post("/admin/api/announcements", json={
            "title": "Plain", "body_markdown": "hello",
            "scheduled_for": _future_iso(),
            "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
        })
        assert r.json()["event_offset_minutes"] == 0


class TestPlaceholderResolutionAtDelivery:
    """The actual point of spec §27: placeholders resolve per-target, at
    send time — not once, at creation."""

    async def test_alliance_name_and_event_time_resolved_on_delivery(
        self, client: AsyncClient, db_session, db_engine, tenant: dict, monkeypatch
    ):
        created = await client.post("/admin/api/announcements", json={
            "title": "Daily Reset Warning",
            "body_markdown": "{alliance_name} ({kingdom_name}): reset {event_time_relative}",
            "scheduled_for": _future_iso(), "event_offset_minutes": 15,
            "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
        })
        announcement_id = created.json()["id"]
        await _backdate_to_due(db_session, announcement_id)

        calls = []
        async def fake_send(token, channel_id, message):
            calls.append(message)
            return True, ""
        monkeypatch.setattr("scheduler.announcements.send_channel_message", fake_send)

        await _run_delivery_job(db_engine)

        assert len(calls) == 1
        assert calls[0].startswith("MOD (Kingdom 138): reset <t:")
        # Never sent to Discord with the literal placeholder still in it.
        assert "{alliance_name}" not in calls[0]
        assert "{event_time_relative}" not in calls[0]

    async def test_each_target_sees_its_own_alliance_name(
        self, client: AsyncClient, db_session, db_engine, tenant: dict, second_tenant: dict, monkeypatch
    ):
        created = await client.post("/admin/api/announcements", json={
            "title": "Kingdom-wide", "body_markdown": "Hello, {alliance_name}!",
            "scheduled_for": _future_iso(),
            "targets": [
                {"tenant_slug": tenant["slug"], "discord_channel_id": "chan-mod"},
                {"tenant_slug": second_tenant["slug"], "discord_channel_id": "chan-nsr"},
            ],
        })
        announcement_id = created.json()["id"]
        await _backdate_to_due(db_session, announcement_id)

        calls = {}
        async def fake_send(token, channel_id, message):
            calls[channel_id] = message
            return True, ""
        monkeypatch.setattr("scheduler.announcements.send_channel_message", fake_send)

        await _run_delivery_job(db_engine)

        assert calls["chan-mod"] == "Hello, MOD!"
        assert calls["chan-nsr"] == "Hello, NSR!"

    async def test_recurring_announcement_gets_a_fresh_timestamp_each_occurrence(
        self, client: AsyncClient, db_session, db_engine, tenant: dict, monkeypatch
    ):
        created = await client.post("/admin/api/announcements", json={
            "title": "Recurring", "body_markdown": "Sent at {send_time}",
            "scheduled_for": _future_iso(), "recurring": True, "interval_days": 1,
            "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
        })
        announcement_id = created.json()["id"]

        calls = []
        async def fake_send(token, channel_id, message):
            calls.append(message)
            return True, ""
        monkeypatch.setattr("scheduler.announcements.send_channel_message", fake_send)

        # Two distinct past times, far enough apart that they can't collide
        # at second resolution (unlike two back-to-back "now - 1 minute"
        # backdates, which can render an identical timestamp if the test
        # runs faster than a second) — simulating this recurring
        # announcement's second occurrence actually being a day later.
        announcement = await db_session.get(Announcement, announcement_id)
        announcement.scheduled_for = datetime.now(timezone.utc) - timedelta(days=2)
        await db_session.commit()
        await _run_delivery_job(db_engine)

        announcement = await db_session.get(Announcement, announcement_id)
        announcement.scheduled_for = datetime.now(timezone.utc) - timedelta(days=1)
        await db_session.commit()
        await _run_delivery_job(db_engine)

        assert len(calls) == 2
        # Each occurrence's own scheduled_for produced a different
        # rendered timestamp — not the same static text baked in once.
        assert calls[0] != calls[1]

    async def test_body_without_placeholders_is_sent_unchanged(
        self, client: AsyncClient, db_session, db_engine, tenant: dict, monkeypatch
    ):
        created = await client.post("/admin/api/announcements", json={
            "title": "Plain", "body_markdown": "**No placeholders here.**",
            "scheduled_for": _future_iso(),
            "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
        })
        announcement_id = created.json()["id"]
        await _backdate_to_due(db_session, announcement_id)

        calls = []
        async def fake_send(token, channel_id, message):
            calls.append(message)
            return True, ""
        monkeypatch.setattr("scheduler.announcements.send_channel_message", fake_send)

        await _run_delivery_job(db_engine)
        assert calls == ["**No placeholders here.**"]
