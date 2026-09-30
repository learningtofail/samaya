"""Tests for spec §32: bulk CSV import/export of alliance-scope events
(routers/admin/events.py), and {placeholder} resolution (spec §27's six
names) now applied to an Event's description wherever it reaches Discord
— the Scheduled Event's own description field and the pre-event ping's
extra line.
"""
from datetime import date, datetime, time, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import EventDefinition, Occurrence

VALID_CSV_HEADER = (
    "name,interval_days,start_time_utc,duration_hours,discord_channel,"
    "description,leadership_only,anchor_date,notification_channel_id,"
    "notification_role_id,notify_minutes_before\n"
)

# csv.writer defaults to \r\n line terminators — what the export endpoint
# actually produces. VALID_CSV_HEADER (plain \n) is used as *import* input
# throughout this file since csv.DictReader accepts either; this second
# constant is only for asserting against the export endpoint's raw output.
EXPORTED_CSV_HEADER = VALID_CSV_HEADER.replace("\n", "\r\n")


class TestBulkExport:

    async def test_export_is_empty_csv_with_header_only(self, client: AsyncClient, tenant: dict):
        r = await client.get("/admin/api/events/export.csv")
        assert r.status_code == 200
        assert r.text == EXPORTED_CSV_HEADER

    async def test_export_includes_own_alliance_scope_events(
        self, client: AsyncClient, tenant: dict
    ):
        await client.post("/admin/api/events", json={
            "name": "Siege Prep", "interval_days": 7, "start_time_utc": "19:00",
            "duration_hours": 1.0, "discord_channel": "#war", "description": "desc",
            "anchor_date": "2025-05-04",
        })
        r = await client.get("/admin/api/events/export.csv")
        assert "Siege Prep" in r.text
        assert r.text.count("\n") == 2  # header + one data row

    async def test_export_excludes_kingdom_wide_events(
        self, client: AsyncClient, tenant: dict
    ):
        await client.post("/admin/api/events", json={
            "name": "KW Event", "interval_days": 7, "start_time_utc": "19:00",
            "duration_hours": 1.0, "scope": "kingdom-wide", "anchor_date": "2025-05-04",
        })
        r = await client.get("/admin/api/events/export.csv")
        assert "KW Event" not in r.text

    async def test_export_scoped_to_current_tenant(
        self, client: AsyncClient, tenant: dict, second_tenant: dict
    ):
        await client.post(
            "/admin/api/events",
            json={
                "name": "MOD Event", "interval_days": 7, "start_time_utc": "19:00",
                "duration_hours": 1.0, "anchor_date": "2025-05-04",
            },
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        r = await client.get("/admin/api/events/export.csv", headers={"X-Tenant-Slug": second_tenant["slug"]})
        assert "MOD Event" not in r.text


class TestBulkImport:

    async def test_import_creates_valid_rows(self, client: AsyncClient, tenant: dict):
        csv_content = VALID_CSV_HEADER + "Siege Prep,7,19:00,1.0,#war,desc,false,2025-05-04,,,\n"
        r = await client.post(
            "/admin/api/events/import.csv",
            files={"file": ("events.csv", csv_content, "text/csv")},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body == {"created": 1, "errors": []}

        events = (await client.get("/admin/api/events")).json()
        assert any(e["name"] == "Siege Prep" and e["scope"] == "alliance" for e in events)

    async def test_import_reports_bad_row_without_aborting_the_file(
        self, client: AsyncClient, tenant: dict
    ):
        csv_content = (
            VALID_CSV_HEADER
            + "Good Event,7,19:00,1.0,#war,desc,false,2025-05-04,,,\n"
            + "Bad Event,notanumber,19:00,1.0,#war,desc,false,2025-05-04,,,\n"
        )
        r = await client.post(
            "/admin/api/events/import.csv",
            files={"file": ("events.csv", csv_content, "text/csv")},
        )
        body = r.json()
        assert body["created"] == 1
        assert len(body["errors"]) == 1
        assert body["errors"][0]["row"] == 3  # header=1, good=2, bad=3

        events = (await client.get("/admin/api/events")).json()
        names = {e["name"] for e in events}
        assert "Good Event" in names
        assert "Bad Event" not in names

    async def test_import_missing_column_is_422(self, client: AsyncClient, tenant: dict):
        r = await client.post(
            "/admin/api/events/import.csv",
            files={"file": ("events.csv", "name,interval_days\nX,7\n", "text/csv")},
        )
        assert r.status_code == 422

    async def test_import_always_creates_alliance_scope(self, client: AsyncClient, tenant: dict):
        # scope isn't even a column — every imported row is alliance-scope
        # regardless, per the endpoint's own docstring.
        csv_content = VALID_CSV_HEADER + "Whatever,7,19:00,1.0,,,false,2025-05-04,,,\n"
        await client.post(
            "/admin/api/events/import.csv",
            files={"file": ("events.csv", csv_content, "text/csv")},
        )
        events = (await client.get("/admin/api/events")).json()
        event = next(e for e in events if e["name"] == "Whatever")
        assert event["scope"] == "alliance"

    async def test_viewer_cannot_import(self, make_user_and_client, tenant: dict):
        client, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")])
        csv_content = VALID_CSV_HEADER + "X,7,19:00,1.0,,,false,2025-05-04,,,\n"
        r = await client.post(
            "/admin/api/events/import.csv",
            files={"file": ("events.csv", csv_content, "text/csv")},
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert r.status_code == 403
        await client.aclose()

    async def test_round_trips_through_export_and_reimport(self, client: AsyncClient, tenant: dict):
        await client.post("/admin/api/events", json={
            "name": "Round Trip", "interval_days": 14, "start_time_utc": "08:30",
            "duration_hours": 2.5, "discord_channel": "#test", "description": "d",
            "leadership_only": True, "anchor_date": "2025-06-01",
            "notification_channel_id": "chan-1", "notification_role_id": "role-1",
            "notify_minutes_before": 15,
        })
        exported = (await client.get("/admin/api/events/export.csv")).text

        r = await client.post(
            "/admin/api/events/import.csv",
            files={"file": ("events.csv", exported, "text/csv")},
        )
        assert r.json() == {"created": 1, "errors": []}

        events = (await client.get("/admin/api/events")).json()
        matches = [e for e in events if e["name"] == "Round Trip"]
        assert len(matches) == 2  # the original plus the re-imported copy
        for e in matches:
            assert e["interval_days"] == 14
            assert e["leadership_only"] is True
            assert e["notify_minutes_before"] == 15


async def _make_pingable_occurrence(db_session: AsyncSession, tenant: dict, description: str):
    today = date.today()
    event = EventDefinition(
        owning_tenant_id=tenant["id"], scope="alliance", name="Siege Prep",
        interval_days=7, start_time_utc=time(19, 0), duration_hours=1.0,
        discord_channel="#alliance-war", description=description, anchor_date=today,
        notification_channel_id="notify-chan", notification_role_id="notify-role",
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
    return event, occ


class TestPingPlaceholders:

    async def test_placeholders_resolved_in_discord_event_description(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, monkeypatch
    ):
        _, occ = await _make_pingable_occurrence(
            db_session, tenant, "Bring gear for {alliance_name}! Starts {event_time_relative}."
        )
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
        assert tenant["slug"] not in captured["description"]  # sanity: not the raw tenant slug
        assert "{alliance_name}" not in captured["description"]
        assert "MOD" in captured["description"]
        assert "{event_time_relative}" not in captured["description"]
        assert "<t:" in captured["description"] and ":R>" in captured["description"]

    async def test_placeholders_resolved_in_pre_event_ping(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, monkeypatch
    ):
        _, occ = await _make_pingable_occurrence(
            db_session, tenant, "Reset in {kingdom_name} approaching."
        )
        sent = {}
        async def fake_create(**kwargs):
            return "discord-event-1", ""
        async def fake_send(token, channel, message):
            sent["message"] = message
            return True, ""
        monkeypatch.setattr("services.discord_posting.create_discord_event", fake_create)
        monkeypatch.setattr("services.discord_posting.send_channel_message", fake_send)

        r = await client.post(f"/admin/api/occurrences/{occ.id}/post")
        assert r.status_code == 200, r.text
        assert "{kingdom_name}" not in sent["message"]
        assert "Kingdom 138" in sent["message"]

    async def test_unrecognized_placeholder_left_untouched(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, monkeypatch
    ):
        _, occ = await _make_pingable_occurrence(db_session, tenant, "See {not_a_real_placeholder} for info")
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
        assert "{not_a_real_placeholder}" in captured["description"]

    async def test_empty_description_stays_empty(
        self, client: AsyncClient, db_session: AsyncSession, tenant: dict, monkeypatch
    ):
        _, occ = await _make_pingable_occurrence(db_session, tenant, "")
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
        assert captured["description"] == ""
