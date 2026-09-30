"""Tests for spec §33's Event cover image (`EventDefinition.cover_image_data`):
validation on create/patch, the PATCH null-vs-empty-string distinction
(null = leave alone, "" = explicitly remove), and that a posted occurrence
passes it through to create_discord_event's new `image` argument.
"""
from datetime import date, datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import EventDefinition, Occurrence

VALID_EVENT = {
    "name":            "Viking Vengeance",
    "interval_days":   14,
    "start_time_utc":  "20:00",
    "duration_hours":  1.0,
    "discord_channel": "#alliance-war",
    "description":     "Biweekly alliance PvP",
    "anchor_date":     "2025-05-04",
}

# A minimal valid 1x1 PNG, base64-encoded, wrapped as the data URI the
# browser's FileReader.readAsDataURL() produces — exactly what the
# validator/endpoint actually receive from the admin UI.
TINY_PNG_DATA_URI = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQV"
    "R42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class TestCreateWithCoverImage:

    async def test_create_stores_cover_image(self, client: AsyncClient, tenant: dict):
        r = await client.post("/admin/api/events", json={**VALID_EVENT, "cover_image_data": TINY_PNG_DATA_URI})
        assert r.status_code == 201, r.text
        assert r.json()["cover_image_data"] == TINY_PNG_DATA_URI

    async def test_create_without_cover_image_is_null(self, client: AsyncClient, tenant: dict):
        r = await client.post("/admin/api/events", json=VALID_EVENT)
        assert r.status_code == 201
        assert r.json()["cover_image_data"] is None

    async def test_rejects_non_image_data_uri(self, client: AsyncClient, tenant: dict):
        r = await client.post("/admin/api/events", json={
            **VALID_EVENT, "cover_image_data": "data:text/plain;base64,aGVsbG8=",
        })
        assert r.status_code == 422

    async def test_rejects_plain_base64_without_data_uri_prefix(self, client: AsyncClient, tenant: dict):
        r = await client.post("/admin/api/events", json={**VALID_EVENT, "cover_image_data": "iVBORw0KGgo="})
        assert r.status_code == 422

    async def test_rejects_oversized_payload(self, client: AsyncClient, tenant: dict):
        huge = "data:image/png;base64," + ("A" * (9 * 1024 * 1024))
        r = await client.post("/admin/api/events", json={**VALID_EVENT, "cover_image_data": huge})
        assert r.status_code == 422

    async def test_empty_string_is_treated_as_no_image(self, client: AsyncClient, tenant: dict):
        r = await client.post("/admin/api/events", json={**VALID_EVENT, "cover_image_data": ""})
        assert r.status_code == 201
        assert r.json()["cover_image_data"] is None


class TestPatchCoverImage:

    async def test_omitting_field_leaves_existing_image_alone(self, client: AsyncClient, tenant: dict):
        created = (await client.post(
            "/admin/api/events", json={**VALID_EVENT, "cover_image_data": TINY_PNG_DATA_URI}
        )).json()
        r = await client.patch(f"/admin/api/events/{created['id']}", json={"name": "Renamed"})
        assert r.status_code == 200, r.text
        assert r.json()["cover_image_data"] == TINY_PNG_DATA_URI

    async def test_explicit_empty_string_removes_the_image(self, client: AsyncClient, tenant: dict):
        created = (await client.post(
            "/admin/api/events", json={**VALID_EVENT, "cover_image_data": TINY_PNG_DATA_URI}
        )).json()
        r = await client.patch(f"/admin/api/events/{created['id']}", json={"cover_image_data": ""})
        assert r.status_code == 200, r.text
        assert r.json()["cover_image_data"] is None

    async def test_patch_can_set_a_new_image(self, client: AsyncClient, tenant: dict):
        created = (await client.post("/admin/api/events", json=VALID_EVENT)).json()
        r = await client.patch(
            f"/admin/api/events/{created['id']}", json={"cover_image_data": TINY_PNG_DATA_URI}
        )
        assert r.status_code == 200, r.text
        assert r.json()["cover_image_data"] == TINY_PNG_DATA_URI

    async def test_viewer_cannot_patch_cover_image(self, make_user_and_client, client: AsyncClient, tenant: dict):
        created = (await client.post("/admin/api/events", json=VALID_EVENT)).json()
        viewer_client, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")])
        r = await viewer_client.patch(
            f"/admin/api/events/{created['id']}",
            json={"cover_image_data": TINY_PNG_DATA_URI},
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert r.status_code == 403
        await viewer_client.aclose()


class TestCoverImagePassedToDiscord:

    async def test_post_occurrence_passes_cover_image_to_create_discord_event(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, monkeypatch
    ):
        event = EventDefinition(
            owning_tenant_id=tenant["id"], scope="alliance", name="Siege Prep",
            interval_days=7, start_time_utc=datetime.now(timezone.utc).time(), duration_hours=1.0,
            discord_channel="#alliance-war", description="", anchor_date=date.today(),
            cover_image_data=TINY_PNG_DATA_URI,
        )
        db_session.add(event)
        await db_session.commit()
        await db_session.refresh(event)

        start_dt = datetime.now(timezone.utc) + timedelta(hours=2)
        occ = Occurrence(
            event_id=event.id, tenant_id=tenant["id"], occurrence_date=start_dt.date(),
            start_datetime_utc=start_dt, end_datetime_utc=start_dt + timedelta(hours=1),
            post_status="pending",
        )
        db_session.add(occ)
        await db_session.commit()
        await db_session.refresh(occ)

        captured = {}
        async def fake_create(**kwargs):
            captured.update(kwargs)
            return "discord-event-1", ""
        async def fake_send(token, channel, message):
            return True, ""
        monkeypatch.setattr("services.discord_posting.create_discord_event", fake_create)
        monkeypatch.setattr("services.discord_posting.send_channel_message", fake_send)

        r = await client.post(f"/admin/api/occurrences/{occ.id}/post")
        assert r.status_code == 200, r.text
        assert captured["image"] == TINY_PNG_DATA_URI

    async def test_post_occurrence_omits_image_when_none_set(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, monkeypatch
    ):
        event = EventDefinition(
            owning_tenant_id=tenant["id"], scope="alliance", name="Siege Prep",
            interval_days=7, start_time_utc=datetime.now(timezone.utc).time(), duration_hours=1.0,
            discord_channel="#alliance-war", description="", anchor_date=date.today(),
        )
        db_session.add(event)
        await db_session.commit()
        await db_session.refresh(event)

        start_dt = datetime.now(timezone.utc) + timedelta(hours=2)
        occ = Occurrence(
            event_id=event.id, tenant_id=tenant["id"], occurrence_date=start_dt.date(),
            start_datetime_utc=start_dt, end_datetime_utc=start_dt + timedelta(hours=1),
            post_status="pending",
        )
        db_session.add(occ)
        await db_session.commit()
        await db_session.refresh(occ)

        captured = {}
        async def fake_create(**kwargs):
            captured.update(kwargs)
            return "discord-event-1", ""
        async def fake_send(token, channel, message):
            return True, ""
        monkeypatch.setattr("services.discord_posting.create_discord_event", fake_create)
        monkeypatch.setattr("services.discord_posting.send_channel_message", fake_send)

        r = await client.post(f"/admin/api/occurrences/{occ.id}/post")
        assert r.status_code == 200, r.text
        assert captured["image"] is None


class TestDiscordApiImageField:

    async def test_create_discord_event_includes_image_when_given(self, monkeypatch):
        import httpx
        from services.discord_api import create_discord_event

        captured = {}
        class FakeResponse:
            status_code = 201
            def json(self): return {"id": "evt-1"}

        async def fake_post(self, url, headers=None, json=None):
            captured.update(json)
            return FakeResponse()

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        await create_discord_event(
            token="t", guild_id="g", name="n",
            start=datetime.now(timezone.utc), end=datetime.now(timezone.utc) + timedelta(hours=1),
            description="d", location="l", image=TINY_PNG_DATA_URI,
        )
        assert captured["image"] == TINY_PNG_DATA_URI

    async def test_create_discord_event_omits_image_when_not_given(self, monkeypatch):
        import httpx
        from services.discord_api import create_discord_event

        captured = {}
        class FakeResponse:
            status_code = 201
            def json(self): return {"id": "evt-1"}

        async def fake_post(self, url, headers=None, json=None):
            captured.update(json)
            return FakeResponse()

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        await create_discord_event(
            token="t", guild_id="g", name="n",
            start=datetime.now(timezone.utc), end=datetime.now(timezone.utc) + timedelta(hours=1),
            description="d", location="l",
        )
        assert "image" not in captured
