"""Spec §70: slash commands through the real /webhooks/discord route, with
requests signed by a throwaway key so the signature path is exercised."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from nacl.signing import SigningKey
from sqlalchemy import select

from models.db import EventOccurrence, TenantSecondaryServer, Ticket
from routers import webhooks
from services import discord_commands, rate_limit
from services.rate_limit import RateLimiter
from tests.unified_helpers import make_event, sync

UTC = timezone.utc
TOMORROW = date.today() + timedelta(days=1)
MOD_GUILD, NSR_GUILD = "test-guild-mod", "test-guild-nsr"


@pytest.fixture
def post(monkeypatch, client_no_session):
    key = SigningKey.generate()
    monkeypatch.setattr(webhooks, "PLATFORM_PUBLIC_KEY", key.verify_key.encode().hex())

    async def send(payload, *, signed=True):
        body = json.dumps(payload).encode()
        timestamp = "1700000000"
        signature = key.sign(timestamp.encode() + body).signature.hex() if signed else "00" * 64
        return await client_no_session.post(
            "/webhooks/discord", content=body,
            headers={"X-Signature-Ed25519": signature, "X-Signature-Timestamp": timestamp, "Content-Type": "application/json"},
        )
    return send


async def _event(sf, tenant, name, **kw):
    kw.setdefault("anchor", TOMORROW)
    kw.setdefault("interval", None)
    kw.setdefault("reminders", ())
    event_id = await make_event(sf, tenant, name=name, **kw)
    await sync(sf, event_id, now=datetime.now(UTC).replace(hour=0, minute=0))
    return event_id


def _command(name, guild=MOD_GUILD, **options):
    return {"type": 2, "guild_id": guild, "member": {"user": {"id": "42", "username": "faysal", "global_name": "Faysal"}},
            "data": {"name": name, "options": [{"name": k, "value": v} for k, v in options.items()]}}


def _text(response):
    return response.json()["data"]["content"]


class TestSignature:
    async def test_a_bad_signature_is_rejected(self, post):
        assert (await post(_command("schedule"), signed=False)).status_code == 401

    async def test_ping_still_answers(self, post):
        assert (await post({"type": 1})).json() == {"type": 1}


class TestSchedule:
    async def test_lists_this_servers_alliance_only(self, post, sf, tenant, second_tenant):
        await _event(sf, tenant, "MOD Rally")
        await _event(sf, second_tenant, "NSR Rally")
        text = _text(await post(_command("schedule")))
        assert "MOD Rally" in text and "NSR Rally" not in text
        assert "<t:" in text and ":R>" in text and "· MOD" in text

    async def test_replies_are_private_and_never_ping(self, post, sf, tenant):
        await _event(sf, tenant, "@everyone Rally")
        data = (await post(_command("schedule"))).json()["data"]
        assert data["flags"] == 64 and data["allowed_mentions"] == {"parse": []}

    async def test_an_unknown_server_gets_every_alliance(self, post, sf, tenant, second_tenant):
        await _event(sf, tenant, "MOD Rally")
        await _event(sf, second_tenant, "NSR Rally")
        text = _text(await post(_command("schedule", guild="somewhere-else")))
        assert "MOD Rally" in text and "NSR Rally" in text

    async def test_a_direct_message_gets_every_alliance(self, post, sf, tenant, second_tenant):
        await _event(sf, tenant, "MOD Rally")
        await _event(sf, second_tenant, "NSR Rally")
        dm = {"type": 2, "user": {"id": "7", "username": "x"}, "data": {"name": "schedule"}}
        text = _text(await post(dm))
        assert "MOD Rally" in text and "NSR Rally" in text

    async def test_the_alliance_option_overrides_the_server(self, post, sf, tenant, second_tenant):
        await _event(sf, tenant, "MOD Rally")
        await _event(sf, second_tenant, "NSR Rally")
        text = _text(await post(_command("schedule", alliance="nsr")))
        assert "NSR Rally" in text and "MOD Rally" not in text

    async def test_an_unknown_alliance_is_named_in_the_reply(self, post, sf, tenant):
        assert 'No alliance named "zzz"' in _text(await post(_command("schedule", alliance="zzz")))

    async def test_a_shared_server_shows_every_declared_alliance(self, post, sf, tenant, second_tenant):
        async with sf() as s:
            s.add(TenantSecondaryServer(tenant_id=second_tenant["id"], server_id=tenant["server_id"]))
            await s.commit()
        await _event(sf, tenant, "MOD Rally")
        await _event(sf, second_tenant, "NSR Rally")
        text = _text(await post(_command("schedule")))
        assert "MOD Rally" in text and "NSR Rally" in text

    async def test_kingdom_wide_events_are_labelled_kingdom(self, post, sf, tenant, second_tenant):
        await _event(sf, tenant, "Council", scope="kingdom-wide")
        for guild in (MOD_GUILD, NSR_GUILD):
            text = _text(await post(_command("schedule", guild=guild)))
            assert "Council" in text and "· Kingdom" in text and "MOD" not in text.split("·")[-1]

    async def test_leadership_only_events_never_appear(self, post, sf, tenant):
        await _event(sf, tenant, "Secret Plan", leadership_only=True)
        assert "Secret Plan" not in _text(await post(_command("schedule")))

    async def test_cancelled_occurrences_are_skipped(self, post, sf, tenant):
        event_id = await _event(sf, tenant, "Called Off")
        async with sf() as s:
            for occ in (await s.execute(select(EventOccurrence).where(EventOccurrence.event_id == event_id))).scalars():
                occ.status = "cancelled"
            await s.commit()
        assert "Called Off" not in _text(await post(_command("schedule")))

    async def test_days_limits_the_window(self, post, sf, tenant):
        await _event(sf, tenant, "Far Away", anchor=date.today() + timedelta(days=10))
        assert "Far Away" not in _text(await post(_command("schedule")))
        assert "Far Away" in _text(await post(_command("schedule", days=14)))

    async def test_nothing_upcoming(self, post, tenant):
        assert _text(await post(_command("schedule"))).startswith("No events in the next 7 days")

    async def test_a_long_list_is_cut_to_fit_discord(self, post, sf, tenant):
        for i in range(60):
            await _event(sf, tenant, f"Event number {i:02d} with a fairly long descriptive name")
        text = _text(await post(_command("schedule")))
        assert len(text) <= 2000 and "more." in text.splitlines()[-1]


class TestNext:
    async def test_returns_the_soonest(self, post, sf, tenant):
        await _event(sf, tenant, "Later", anchor=date.today() + timedelta(days=3))
        await _event(sf, tenant, "Sooner")
        text = _text(await post(_command("next")))
        assert "Sooner" in text and "Later" not in text

    async def test_none_upcoming(self, post, tenant):
        assert _text(await post(_command("next"))).startswith("No events in the next")


class TestAutocomplete:
    async def test_offers_alliances_matching_what_was_typed(self, post, tenant, second_tenant):
        payload = {"type": 4, "guild_id": MOD_GUILD, "data": {"name": "schedule", "options": [
            {"name": "alliance", "value": "ns", "focused": True}]}}
        body = (await post(payload)).json()
        assert body["type"] == 8 and body["data"]["choices"] == [{"name": "NSR", "value": "nsr"}]

    async def test_empty_input_lists_the_kingdoms_alliances(self, post, tenant, second_tenant):
        payload = {"type": 4, "guild_id": MOD_GUILD, "data": {"name": "next", "options": [
            {"name": "alliance", "value": "", "focused": True}]}}
        assert [c["value"] for c in (await post(payload)).json()["data"]["choices"]] == ["mod", "nsr"]


def _submit(category="Bug", title="Reminder was late", description="It posted 5 minutes after the start.", guild=MOD_GUILD):
    return {"type": 5, "guild_id": guild, "member": {"user": {"id": "42", "username": "faysal", "global_name": "Faysal"}},
            "data": {"custom_id": f"feedback:{category}", "components": [
                {"type": 1, "components": [{"type": 4, "custom_id": "title", "value": title}]},
                {"type": 1, "components": [{"type": 4, "custom_id": "description", "value": description}]},
            ]}}


class TestFeedback:
    async def test_the_command_opens_a_modal_within_discords_limits(self, post, tenant):
        body = (await post(_command("feedback", category="Suggestion"))).json()
        data = body["data"]
        assert body["type"] == 9 and data["custom_id"] == "feedback:Suggestion" and len(data["title"]) <= 45
        fields = [row["components"][0] for row in data["components"]]
        assert [f["custom_id"] for f in fields] == ["title", "description"]
        assert all(len(f["label"]) <= 45 for f in fields)

    async def test_submitting_files_a_ticket_for_the_servers_alliance(self, client_no_session, post, tenant, second_tenant, db_engine):
        text = _text(await post(_submit()))
        assert "Thanks, feedback #" in text
        listed = (await client_no_session.get("/api/tickets")).json()["active"]
        assert listed[0]["title"] == "Reminder was late" and "submitter_contact" not in listed[0]
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
        async with async_sessionmaker(db_engine, class_=AsyncSession)() as s:
            ticket = (await s.execute(select(Ticket))).scalar_one()
        assert ticket.kind == "feedback" and ticket.error_type == "Bug" and ticket.tenant_id == tenant["id"]
        assert ticket.submitter_contact == "Discord: Faysal (42)"

    async def test_an_unmapped_server_files_no_alliance(self, post, db_engine, tenant):
        await post(_submit(guild="elsewhere"))
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
        async with async_sessionmaker(db_engine, class_=AsyncSession)() as s:
            assert (await s.execute(select(Ticket))).scalar_one().tenant_id is None

    async def test_blank_or_bad_submissions_file_nothing(self, post, db_engine, tenant):
        assert "incomplete" in _text(await post(_submit(title="  ")))
        assert "incomplete" in _text(await post(_submit(category="Nope")))
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
        async with async_sessionmaker(db_engine, class_=AsyncSession)() as s:
            assert (await s.execute(select(Ticket))).first() is None

    async def test_a_user_is_limited_to_three_tickets(self, monkeypatch, post, tenant):
        monkeypatch.setattr(rate_limit, "DISABLED", False)
        monkeypatch.setattr(discord_commands, "_feedback_limit", RateLimiter("t", 3, 600))
        replies = [_text(await post(_submit(title=f"Ticket {i}"))) for i in range(4)]
        assert all("Thanks" in r for r in replies[:3]) and "several already" in replies[3]


class TestDispatch:
    async def test_an_unknown_command_is_answered(self, post):
        assert _text(await post(_command("nonsense"))) == "Unknown command."

    async def test_a_failure_gives_an_apology_not_an_error(self, monkeypatch, post):
        async def boom(*a, **k):
            raise RuntimeError("db down")
        monkeypatch.setattr(discord_commands, "handle_schedule", boom)
        assert "Something went wrong" in _text(await post(_command("schedule")))


class TestCommandDefinitions:
    def test_names_and_option_limits(self):
        by_name = {c["name"]: c for c in discord_commands.COMMANDS}
        assert set(by_name) == {"schedule", "next", "feedback"}
        days = next(o for o in by_name["schedule"]["options"] if o["name"] == "days")
        assert (days["min_value"], days["max_value"]) == (1, discord_commands.MAX_DAYS)
        assert [c["value"] for c in by_name["feedback"]["options"][0]["choices"]] == list(discord_commands.FEEDBACK_CATEGORIES)
        assert all(len(c["description"]) <= 100 for c in discord_commands.COMMANDS)
