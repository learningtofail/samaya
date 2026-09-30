"""Tests for spec §20 — explicit extra (tenant, channel, role) targets on
an event, independent of `scope`'s automatic same-Kingdom fan-out. Covers
the API surface (routers/admin/events.py's targets field), the posting
fan-out generalization, the notification-resolution generalization, and
access control (both reuse routers/admin/deps.py's resolve_target_tenants,
shared with Announcements).
"""
from datetime import date, datetime, time, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import DiscordServer, EventDefinition, EventTarget, Kingdom, Occurrence, PostLog, Tenant


async def _third_tenant(db_session: AsyncSession) -> dict:
    """A tenant in a *different* Kingdom from `tenant`/`second_tenant` —
    the HTD case: a target that isn't reachable via kingdom-wide's
    automatic fan-out no matter what, since it's not even in the same
    Kingdom row."""
    kingdom = Kingdom(name="Kingdom 999", slug="k999")
    db_session.add(kingdom)
    await db_session.commit()
    server = DiscordServer(
        name="HTD's server", guild_id="test-guild-htd", bot_token="test-bot-token-htd", public_key="test-pubkey-htd",
    )
    db_session.add(server)
    await db_session.commit()
    t = Tenant(kingdom_id=kingdom.id, server_id=server.id, name="HTD", slug="htd")
    db_session.add(t)
    await db_session.commit()
    await db_session.refresh(t)
    return {"id": t.id, "slug": t.slug, "kingdom_id": kingdom.id}


class TestEventTargetsApi:

    async def test_create_event_with_target(self, client: AsyncClient, tenant: dict, db_session: AsyncSession):
        htd = await _third_tenant(db_session)
        r = await client.post("/admin/api/events", json={
            "name": "Cross-Server Rally", "interval_days": 7, "start_time_utc": "19:00",
            "duration_hours": 1.0, "anchor_date": str(date.today()),
            "targets": [{"tenant_slug": "htd", "notification_channel_id": "chan-htd", "notification_role_id": "role-htd"}],
        })
        assert r.status_code == 201, r.text
        body = r.json()
        assert len(body["targets"]) == 1
        assert body["targets"][0]["tenant_id"] == htd["id"]
        assert body["targets"][0]["notification_channel_id"] == "chan-htd"

    async def test_unknown_target_slug_rejected(self, client: AsyncClient, tenant: dict):
        r = await client.post("/admin/api/events", json={
            "name": "Bad Target", "interval_days": 7, "start_time_utc": "19:00",
            "duration_hours": 1.0, "anchor_date": str(date.today()),
            "targets": [{"tenant_slug": "does-not-exist", "notification_channel_id": "x"}],
        })
        assert r.status_code == 404

    async def test_coordinator_cannot_target_inaccessible_tenant(
        self, tenant: dict, db_session: AsyncSession, make_user_and_client
    ):
        htd = await _third_tenant(db_session)
        coordinator, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "owner")])
        r = await coordinator.post(
            "/admin/api/events",
            json={
                "name": "No Access", "interval_days": 7, "start_time_utc": "19:00",
                "duration_hours": 1.0, "anchor_date": str(date.today()),
                "targets": [{"tenant_slug": htd["slug"], "notification_channel_id": "x"}],
            },
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert r.status_code == 403
        await coordinator.aclose()

    async def test_patch_replaces_target_list(self, client: AsyncClient, tenant: dict, db_session: AsyncSession):
        await _third_tenant(db_session)
        create = await client.post("/admin/api/events", json={
            "name": "Replaceable", "interval_days": 7, "start_time_utc": "19:00",
            "duration_hours": 1.0, "anchor_date": str(date.today()),
            "targets": [{"tenant_slug": "htd", "notification_channel_id": "old-chan"}],
        })
        event_id = create.json()["id"]

        patch = await client.patch(f"/admin/api/events/{event_id}", json={
            "targets": [{"tenant_slug": "htd", "notification_channel_id": "new-chan", "notification_role_id": "new-role"}],
        })
        assert patch.status_code == 200, patch.text
        targets = patch.json()["targets"]
        assert len(targets) == 1
        assert targets[0]["notification_channel_id"] == "new-chan"
        assert targets[0]["notification_role_id"] == "new-role"

    async def test_patch_with_empty_targets_clears_list(self, client: AsyncClient, tenant: dict, db_session: AsyncSession):
        await _third_tenant(db_session)
        create = await client.post("/admin/api/events", json={
            "name": "Clearable", "interval_days": 7, "start_time_utc": "19:00",
            "duration_hours": 1.0, "anchor_date": str(date.today()),
            "targets": [{"tenant_slug": "htd", "notification_channel_id": "chan"}],
        })
        event_id = create.json()["id"]

        patch = await client.patch(f"/admin/api/events/{event_id}", json={"targets": []})
        assert patch.status_code == 200
        assert patch.json()["targets"] == []


class TestEventTargetsPosting:

    async def test_alliance_event_posts_to_explicit_target_too(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, monkeypatch
    ):
        htd = await _third_tenant(db_session)
        today = date.today()
        event = EventDefinition(
            owning_tenant_id=tenant["id"], scope="alliance",
            name="Rally Point", interval_days=7, start_time_utc=time(19, 0),
            duration_hours=1.0, anchor_date=today,
            notification_channel_id="mod-chan", notification_role_id="mod-role",
        )
        db_session.add(event)
        await db_session.commit()
        await db_session.refresh(event)
        db_session.add(EventTarget(
            event_id=event.id, tenant_id=htd["id"],
            notification_channel_id="htd-chan", notification_role_id="htd-role",
        ))
        await db_session.commit()

        start_dt = datetime.now(timezone.utc) + timedelta(hours=2)
        occ = Occurrence(
            event_id=event.id, tenant_id=tenant["id"],
            occurrence_date=start_dt.date(), start_datetime_utc=start_dt,
            end_datetime_utc=start_dt + timedelta(hours=1), post_status="pending",
        )
        db_session.add(occ)
        await db_session.commit()
        await db_session.refresh(occ)

        pinged = []
        async def fake_create(**kwargs):
            return f"discord-event-{kwargs['guild_id']}", ""
        async def fake_send(token, channel, message):
            pinged.append(channel)
            return True, ""
        monkeypatch.setattr("services.discord_posting.create_discord_event", fake_create)
        monkeypatch.setattr("services.discord_posting.send_channel_message", fake_send)

        r = await client.post(f"/admin/api/occurrences/{occ.id}/post")
        assert r.status_code == 200, r.text
        body = r.json()
        assert {t["tenant_slug"] for t in body["targets"]} == {"mod", "htd"}
        assert {t["status"] for t in body["targets"]} == {"posted"}

        logs = (await db_session.execute(select(PostLog))).scalars().all()
        assert {log.tenant_id for log in logs} == {tenant["id"], htd["id"]}
        assert set(pinged) == {"mod-chan", "htd-chan"}

    async def test_kingdom_wide_target_already_in_kingdom_is_not_double_posted(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, second_tenant: dict, monkeypatch
    ):
        """second_tenant (NSR) is already reached by kingdom-wide's
        automatic fan-out — also listing it as an explicit EventTarget
        must not produce two PostLog rows for it."""
        today = date.today()
        event = EventDefinition(
            owning_tenant_id=tenant["id"], scope="kingdom-wide",
            name="Dedup Check", interval_days=14, start_time_utc=time(18, 0),
            duration_hours=3.0, anchor_date=today,
            notification_channel_id="", notification_role_id="",
        )
        db_session.add(event)
        await db_session.commit()
        await db_session.refresh(event)
        db_session.add(EventTarget(event_id=event.id, tenant_id=second_tenant["id"], notification_channel_id="dup-chan"))
        await db_session.commit()

        start_dt = datetime.now(timezone.utc) + timedelta(hours=2)
        occ = Occurrence(
            event_id=event.id, tenant_id=tenant["id"],
            occurrence_date=start_dt.date(), start_datetime_utc=start_dt,
            end_datetime_utc=start_dt + timedelta(hours=3), post_status="pending",
        )
        db_session.add(occ)
        await db_session.commit()
        await db_session.refresh(occ)

        async def fake_create(**kwargs):
            return f"discord-event-{kwargs['guild_id']}", ""
        monkeypatch.setattr("services.discord_posting.create_discord_event", fake_create)

        r = await client.post(f"/admin/api/occurrences/{occ.id}/post")
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["targets"]) == 2  # mod + nsr, not three

        logs = (await db_session.execute(select(PostLog))).scalars().all()
        assert len(logs) == 2
        assert {log.tenant_id for log in logs} == {tenant["id"], second_tenant["id"]}

    async def test_target_outside_kingdom_gets_its_own_notification(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, monkeypatch
    ):
        """The owning tenant's own notification_channel_id must not leak
        onto an explicit target's ping — each target's own row governs
        its own channel/role, same isolation EventTenantNotification
        already guarantees for kingdom-wide fan-out."""
        htd = await _third_tenant(db_session)
        today = date.today()
        event = EventDefinition(
            owning_tenant_id=tenant["id"], scope="alliance",
            name="Isolated Ping", interval_days=7, start_time_utc=time(19, 0),
            duration_hours=1.0, anchor_date=today,
            notification_channel_id="mod-chan", notification_role_id="mod-role",
        )
        db_session.add(event)
        await db_session.commit()
        await db_session.refresh(event)
        db_session.add(EventTarget(event_id=event.id, tenant_id=htd["id"], notification_channel_id="htd-chan", notification_role_id="htd-role"))
        await db_session.commit()

        start_dt = datetime.now(timezone.utc) + timedelta(hours=2)
        occ = Occurrence(
            event_id=event.id, tenant_id=tenant["id"],
            occurrence_date=start_dt.date(), start_datetime_utc=start_dt,
            end_datetime_utc=start_dt + timedelta(hours=1), post_status="pending",
        )
        db_session.add(occ)
        await db_session.commit()
        await db_session.refresh(occ)

        sent = {}
        async def fake_create(**kwargs):
            return f"discord-event-{kwargs['guild_id']}", ""
        async def fake_send(token, channel, message):
            sent[channel] = message
            return True, ""
        monkeypatch.setattr("services.discord_posting.create_discord_event", fake_create)
        monkeypatch.setattr("services.discord_posting.send_channel_message", fake_send)

        r = await client.post(f"/admin/api/occurrences/{occ.id}/post")
        assert r.status_code == 200, r.text

        assert "mod-chan" in sent and "<@&mod-role>" in sent["mod-chan"]
        assert "htd-chan" in sent and "<@&htd-role>" in sent["htd-chan"]
