"""Spec §84: the admin poll endpoints. Validation, scope, permissions, audit and what reaches Discord."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import routers.admin.polls as polls_router
from models.db import AuditLog, TimePoll, TimePollVote
from services.discord_client import get_discord
from services.time_poll import MAX_OPEN_PER_ALLIANCE, record_vote
from tests.unified_helpers import add_audience, make_event, sync

A = "/admin/api/time-polls"
UTC = timezone.utc
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
H = {"X-Tenant-Slug": "mod"}


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    monkeypatch.setattr(polls_router, "_now", lambda: NOW)


@pytest.fixture
def discord(fake):
    from main import app
    app.dependency_overrides[get_discord] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_discord, None)


def slots(n=3, day=1):
    return [(NOW + timedelta(days=day, hours=i)).isoformat() for i in range(n)]


async def audience_id(sf, tenant):
    from models.db import Audience
    async with sf() as s:
        return (await s.execute(select(Audience.id).where(Audience.kingdom_id == tenant["kingdom_id"]))).scalars().first()


async def create(client, sf, tenant, headers=H, **overrides):
    body = {"title": "Bear Hunt time", "slots": slots(), "audience_ids": [await audience_id(sf, tenant)], **overrides}
    return await client.post(A, json=body, headers=headers)


class TestCreate:
    async def test_creates_posts_and_returns_the_tally(self, client, sf, configured, discord):
        response = await create(client, sf, configured)
        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "open" and body["scope"] == "alliance" and body["alliance"] == "mod" and body["discord_errors"] == []
        assert [s["votes"] for s in body["slots"]] == [0, 0, 0] and len(body["messages"]) == 1
        assert discord.count("send") == 1 and len(next(iter(discord.components.values()))[0]["components"]) == 3

    async def test_closes_in_48_hours_by_default(self, client, sf, configured, discord):
        body = (await create(client, sf, configured)).json()
        assert datetime.fromisoformat(body["closes_at"]) == NOW + timedelta(hours=48)
        assert datetime.fromisoformat((await create(client, sf, configured, closes_in_hours=5)).json()["closes_at"]) == NOW + timedelta(hours=5)

    @pytest.mark.parametrize("overrides", [
        {"slots": slots(1)}, {"slots": slots(7)}, {"title": ""}, {"title": "   "}, {"title": "x" * 81},
        {"closes_in_hours": 0}, {"closes_in_hours": 400}, {"audience_ids": []},
    ])
    async def test_validation_is_a_422(self, client, sf, configured, discord, overrides):
        assert (await create(client, sf, configured, **overrides)).status_code == 422

    async def test_past_slots_are_refused(self, client, sf, configured, discord):
        past = [(NOW - timedelta(hours=1)).isoformat(), (NOW + timedelta(days=1)).isoformat()]
        response = await create(client, sf, configured, slots=past)
        assert response.status_code == 422 and "future" in response.json()["detail"]

    async def test_duplicate_slots_are_refused(self, client, sf, configured, discord):
        same = (NOW + timedelta(days=1)).isoformat()
        assert (await create(client, sf, configured, slots=[same, same.replace(":00+", ":00.000+")])).status_code == 422

    async def test_an_unknown_audience_is_refused(self, client, sf, configured, discord):
        assert (await create(client, sf, configured, audience_ids=[999])).status_code == 422

    async def test_another_alliances_audience_is_refused(self, client, sf, configured, second_tenant, discord):
        other = await add_audience(sf, second_tenant, "chan-nsr", label="NSR only")
        response = await create(client, sf, configured, audience_ids=[other])
        assert response.status_code == 422 and "not linked" in response.json()["detail"]

    async def test_three_open_polls_at_most(self, client, sf, configured, discord):
        for _ in range(MAX_OPEN_PER_ALLIANCE):
            assert (await create(client, sf, configured)).status_code == 201
        response = await create(client, sf, configured)
        assert response.status_code == 409 and "open at once" in response.json()["detail"]

    async def test_a_closed_poll_frees_a_place(self, client, sf, configured, discord):
        ids = [(await create(client, sf, configured)).json()["id"] for _ in range(MAX_OPEN_PER_ALLIANCE)]
        await client.post(f"{A}/{ids[0]}/close", headers=H)
        assert (await create(client, sf, configured)).status_code == 201

    async def test_a_failed_post_still_creates_the_poll_and_says_why(self, client, sf, configured, discord):
        discord.post_error = "403 Missing permissions"
        body = (await create(client, sf, configured)).json()
        assert body["messages"] == [] and "403" in body["discord_errors"][0]
        assert len((await client.get(A, headers=H)).json()) == 1

    async def test_a_paused_destination_is_skipped_and_reported(self, client, sf, configured, discord):
        from models.db import AudienceDestination
        async with sf() as s:
            dest = (await s.execute(select(AudienceDestination))).scalars().first()
            dest.paused_at = NOW
            await s.commit()
        body = (await create(client, sf, configured)).json()
        assert discord.count("send") == 0 and "paused" in body["discord_errors"][0]

    async def test_a_shared_channel_gets_one_message(self, client, sf, configured, discord):
        from models.db import Audience, AudienceDestination
        async with sf() as s:
            second = Audience(kingdom_id=configured["kingdom_id"], label="Same channel")
            second.destinations = [AudienceDestination(server_id=configured["server_id"], channel_id="chan-mod")]
            s.add(second)
            await s.commit()
        from tests.unified_helpers import link_audience
        async with sf() as s:
            second_id = (await s.execute(select(Audience.id).where(Audience.label == "Same channel"))).scalar_one()
        await link_audience(sf, second_id, configured["id"])
        ids = [await audience_id(sf, configured), second_id]
        await create(client, sf, configured, audience_ids=ids)
        assert discord.count("send") == 1

    async def test_the_occurrence_must_be_the_alliances_own(self, client, sf, configured, second_tenant, discord):
        from models.db import EventOccurrence
        event_id = await make_event(sf, second_tenant, interval=None, anchor=NOW.date() + timedelta(days=2))
        await sync(sf, event_id, NOW)
        async with sf() as s:
            occ_id = (await s.execute(select(EventOccurrence.id).where(EventOccurrence.event_id == event_id))).scalar_one()
        assert (await create(client, sf, configured, occurrence_id=occ_id)).status_code == 422
        own = await make_event(sf, configured, interval=None, anchor=NOW.date() + timedelta(days=2))
        await sync(sf, own, NOW)
        async with sf() as s:
            own_occ = (await s.execute(select(EventOccurrence.id).where(EventOccurrence.event_id == own))).scalar_one()
        assert (await create(client, sf, configured, occurrence_id=own_occ)).json()["occurrence_id"] == own_occ

    async def test_creating_is_audited(self, client, sf, configured, discord):
        poll_id = (await create(client, sf, configured)).json()["id"]
        async with sf() as s:
            row = (await s.execute(select(AuditLog).where(AuditLog.table_name == "time_polls"))).scalars().one()
        assert row.row_id == poll_id and row.action == "create"


class TestPermissions:
    async def test_a_viewer_can_read_but_not_write(self, make_user_and_client, client, sf, configured, discord):
        poll_id = (await create(client, sf, configured)).json()["id"]
        viewer, _ = await make_user_and_client(tenant_grants=[(configured["id"], "viewer")])
        assert (await viewer.get(A, headers=H)).status_code == 200 and (await viewer.get(f"{A}/{poll_id}", headers=H)).status_code == 200
        assert (await create(viewer, sf, configured)).status_code == 403
        assert (await viewer.post(f"{A}/{poll_id}/close", headers=H)).status_code == 403
        assert (await viewer.post(f"{A}/{poll_id}/cancel", headers=H)).status_code == 403

    async def test_an_owner_can_manage_their_alliances_polls(self, make_user_and_client, sf, configured, discord):
        owner, _ = await make_user_and_client(tenant_grants=[(configured["id"], "owner")])
        poll_id = (await create(owner, sf, configured)).json()["id"]
        assert (await owner.post(f"{A}/{poll_id}/close", headers=H)).json()["status"] == "closed"

    async def test_a_kingdom_wide_poll_needs_a_kingdom_coordinator(self, make_user_and_client, client, sf, configured, discord):
        owner, _ = await make_user_and_client(tenant_grants=[(configured["id"], "owner")])
        assert (await create(owner, sf, configured, scope="kingdom-wide")).status_code == 403
        coordinator, _ = await make_user_and_client(tenant_grants=[(configured["id"], "owner")], kingdom_grants=[configured["kingdom_id"]], discord_id="coord")
        response = await create(coordinator, sf, configured, scope="kingdom-wide")
        assert response.status_code == 201 and response.json()["scope"] == "kingdom-wide" and response.json()["alliance"] is None

    async def test_a_kingdom_wide_poll_is_visible_to_every_alliance_and_closable_only_by_a_coordinator(
            self, make_user_and_client, client, sf, configured, second_tenant, discord):
        poll_id = (await create(client, sf, configured, scope="kingdom-wide")).json()["id"]
        other, _ = await make_user_and_client(tenant_grants=[(second_tenant["id"], "owner")])
        assert (await other.get(f"{A}/{poll_id}", headers={"X-Tenant-Slug": "nsr"})).status_code == 200
        assert (await other.post(f"{A}/{poll_id}/close", headers={"X-Tenant-Slug": "nsr"})).status_code == 403

    async def test_another_alliances_poll_is_invisible_and_untouchable(self, make_user_and_client, client, sf, configured, second_tenant, discord):
        poll_id = (await create(client, sf, configured)).json()["id"]
        other, _ = await make_user_and_client(tenant_grants=[(second_tenant["id"], "owner")])
        nsr = {"X-Tenant-Slug": "nsr"}
        assert (await other.get(f"{A}/{poll_id}", headers=nsr)).status_code == 404
        assert (await other.get(A, headers=nsr)).json() == []
        assert (await other.post(f"{A}/{poll_id}/close", headers=nsr)).status_code == 404
        assert (await other.post(f"{A}/{poll_id}/cancel", headers=nsr)).status_code == 404

    async def test_signed_out_is_refused(self, client_no_session, configured):
        assert (await client_no_session.get(A, headers=H)).status_code in (401, 403)

    async def test_nothing_on_public_routes(self, client_no_session):
        for path in ("/api/time-polls", "/api/events", "/events.ics"):
            assert "poll" not in (await client_no_session.get(path)).text.lower()


class TestListAndGet:
    async def test_lists_newest_first_and_filters_by_status(self, client, sf, configured, discord):
        first = (await create(client, sf, configured, title="First")).json()["id"]
        await create(client, sf, configured, title="Second")
        await client.post(f"{A}/{first}/cancel", headers=H)
        titles = [p["title"] for p in (await client.get(A, headers=H)).json()]
        assert titles == ["Second", "First"]
        assert [p["title"] for p in (await client.get(f"{A}?status=open", headers=H)).json()] == ["Second"]
        assert (await client.get(f"{A}?status=bogus", headers=H)).status_code == 422

    async def test_the_tally_marks_the_leader_and_never_names_voters(self, client, sf, configured, discord):
        body = (await create(client, sf, configured)).json()
        poll_id, slot_id = body["id"], body["slots"][1]["id"]
        for user in ("1", "2", "3"):
            async with sf() as s:
                await record_vote(s, poll_id, slot_id, user, NOW)
        got = (await client.get(f"{A}/{poll_id}", headers=H)).json()
        assert got["total_votes"] == 3 and [s["leading"] for s in got["slots"]] == [False, True, False]
        assert "voter" not in str(got).lower()

    async def test_a_missing_poll_is_a_404(self, client, configured):
        assert (await client.get(f"{A}/99999", headers=H)).status_code == 404


class TestCloseAndCancel:
    async def test_close_edits_the_message_and_removes_buttons(self, client, sf, configured, discord):
        body = (await create(client, sf, configured)).json()
        closed = (await client.post(f"{A}/{body['id']}/close", headers=H)).json()
        assert closed["status"] == "closed" and closed["closed_at"] and closed["winner_slot_id"] is None
        assert next(iter(discord.components.values())) == [] and "closed" in next(iter(discord.messages.values()))

    async def test_close_can_choose_a_winner_from_this_poll_only(self, client, sf, configured, discord):
        one = (await create(client, sf, configured)).json()
        two = (await create(client, sf, configured)).json()
        refused = await client.post(f"{A}/{one['id']}/close", json={"winner_slot_id": two["slots"][0]["id"]}, headers=H)
        assert refused.status_code == 422
        chosen = (await client.post(f"{A}/{one['id']}/close", json={"winner_slot_id": one["slots"][2]["id"]}, headers=H)).json()
        assert chosen["winner_slot_id"] == one["slots"][2]["id"] and "✅" in next(m for m in discord.messages.values() if "✅" in m)

    async def test_closing_twice_is_a_409(self, client, sf, configured, discord):
        poll_id = (await create(client, sf, configured)).json()["id"]
        await client.post(f"{A}/{poll_id}/close", headers=H)
        assert (await client.post(f"{A}/{poll_id}/close", headers=H)).status_code == 409
        assert (await client.post(f"{A}/{poll_id}/cancel", headers=H)).status_code == 409

    async def test_cancel_marks_the_message_and_keeps_the_votes(self, client, sf, configured, discord):
        body = (await create(client, sf, configured)).json()
        async with sf() as s:
            await record_vote(s, body["id"], body["slots"][0]["id"], "1", NOW)
        assert (await client.post(f"{A}/{body['id']}/cancel", headers=H)).json()["status"] == "cancelled"
        assert "cancelled" in next(iter(discord.messages.values()))
        async with sf() as s:
            assert len((await s.execute(select(TimePollVote))).scalars().all()) == 1

    async def test_close_and_cancel_are_audited(self, client, sf, configured, discord):
        one, two = (await create(client, sf, configured)).json()["id"], (await create(client, sf, configured)).json()["id"]
        await client.post(f"{A}/{one}/close", headers=H)
        await client.post(f"{A}/{two}/cancel", headers=H)
        async with sf() as s:
            rows = (await s.execute(select(AuditLog).where(AuditLog.table_name == "time_polls", AuditLog.action == "update"))).scalars().all()
        assert len(rows) == 2 and {'"closed"' in r.after or '"cancelled"' in r.after for r in rows} == {True}

    async def test_a_missing_poll_is_a_404(self, client, configured, discord):
        assert (await client.post(f"{A}/99999/close", headers=H)).status_code == 404
        assert (await client.post(f"{A}/99999/cancel", headers=H)).status_code == 404

    async def test_the_poll_row_itself_is_stored_as_open(self, client, sf, configured, discord):
        poll_id = (await create(client, sf, configured)).json()["id"]
        async with sf() as s:
            assert (await s.get(TimePoll, poll_id)).status == "open"
