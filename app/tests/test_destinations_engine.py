"""Spec §67: destination resolution, deduplication and conflicts in the
delivery engine, against a fake Discord client."""
from datetime import datetime

from sqlalchemy import select

from models.db import AudienceGroup, AudienceGroupDestination, Delivery, EventAlliance, EventGroup
from services.event_engine import run_delivery_tick
from tests.unified_helpers import (
    NOW, UTC, add_destination, change_destination, deliveries, make_event, sync,
)

AT = datetime(2026, 10, 2, 18, 0, 30, tzinfo=UTC)  # an hour before the 19:00 start, 30s in


async def _shared_channel_setup(sf, tenant, second_tenant, **event_kw):
    """MOD and NSR both post to channel `shared` on MOD's server, each with its own role."""
    await add_destination(sf, tenant, "shared", "role-mod")
    await add_destination(sf, second_tenant, "shared", "role-nsr", server_id=tenant["server_id"])
    return await make_event(sf, tenant, audience=[tenant["id"], second_tenant["id"]], **event_kw)


class TestSharedChannelIsOneSend:
    async def test_one_message_for_two_alliances(self, sf, fake, tenant, second_tenant):
        event_id = await _shared_channel_setup(sf, tenant, second_tenant, message="Bear for {alliance_name}", mention_role=True, interval=None)
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, AT)

        sends = [c for c in fake.calls if c[0] == "send"]
        assert len(sends) == 1
        assert sends[0][1] == "shared"
        assert sends[0][2] == "<@&role-mod> <@&role-nsr> Bear for MOD, NSR"

        rows = await deliveries(sf, event_id, kind="reminder")
        posted = [r for r in rows if r.status == "posted"]
        merged = [r for r in rows if r.status == "cancelled"]
        assert len(posted) == 1 and len(merged) == 1
        assert merged[0].merged_into_id == posted[0].id
        assert merged[0].detail == f"Merged into delivery #{posted[0].id}"

    async def test_a_second_tick_sends_nothing_more(self, sf, fake, tenant, second_tenant):
        event_id = await _shared_channel_setup(sf, tenant, second_tenant)
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, AT)
        await run_delivery_tick(sf, fake, AT)
        assert fake.count("send") == 1

    async def test_different_offsets_stay_separate_reminders(self, sf, fake, tenant, second_tenant):
        event_id = await _shared_channel_setup(sf, tenant, second_tenant, reminders=(60, 10))
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, datetime(2026, 10, 2, 18, 55, tzinfo=UTC))
        assert fake.count("send") == 2

    async def test_the_same_channel_name_on_another_server_is_not_merged(self, sf, fake, tenant, second_tenant):
        await add_destination(sf, tenant, "shared")
        await add_destination(sf, second_tenant, "shared")  # NSR's own server
        event_id = await make_event(sf, tenant, audience=[tenant["id"], second_tenant["id"]])
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, AT)
        assert fake.count("send") == 2

    async def test_distinct_channels_get_their_own_messages(self, sf, fake, tenant, second_tenant):
        await add_destination(sf, tenant, "chan-a")
        await add_destination(sf, second_tenant, "chan-b")
        event_id = await make_event(sf, tenant, audience=[tenant["id"], second_tenant["id"]])
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, AT)
        assert sorted(c[1] for c in fake.calls if c[0] == "send") == ["chan-a", "chan-b"]

    async def test_a_destination_joining_an_already_sent_message_is_not_sent_again(self, sf, fake, tenant, second_tenant):
        await add_destination(sf, tenant, "shared", "role-mod")
        event_id = await make_event(sf, tenant, audience=[tenant["id"], second_tenant["id"]])
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, AT)
        await add_destination(sf, second_tenant, "shared", server_id=tenant["server_id"])
        await sync(sf, event_id, now=AT)
        await run_delivery_tick(sf, fake, AT)
        assert fake.count("send") == 1
        late = [r for r in await deliveries(sf, event_id, kind="reminder") if r.destination_id and r.status == "cancelled"]
        assert late  # the newcomer is recorded as not sent


class TestResolution:
    async def test_default_destinations_of_every_audience_alliance(self, sf, fake, tenant, second_tenant):
        await add_destination(sf, tenant, "a")
        await add_destination(sf, tenant, "extra", label="Extra", default=False)
        await add_destination(sf, second_tenant, "b")
        event_id = await make_event(sf, tenant, audience=[tenant["id"], second_tenant["id"]])
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, AT)
        assert sorted(c[1] for c in fake.calls if c[0] == "send") == ["a", "b"]

    async def test_opt_out_and_add(self, sf, fake, tenant):
        default = await add_destination(sf, tenant, "a")
        extra = await add_destination(sf, tenant, "extra", label="Extra", default=False)
        event_id = await make_event(sf, tenant)
        await change_destination(sf, event_id, default, False)
        await change_destination(sf, event_id, extra, True)
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, AT)
        assert [c[1] for c in fake.calls if c[0] == "send"] == ["extra"]

    async def test_group_expands_into_its_destinations_and_follows_edits(self, sf, fake, tenant, second_tenant):
        mine = await add_destination(sf, tenant, "mine")
        theirs = await add_destination(sf, second_tenant, "theirs", label="Theirs", default=False)
        async with sf() as s:
            group = AudienceGroup(kingdom_id=tenant["kingdom_id"], name="Everyone")
            s.add(group)
            await s.flush()
            s.add(AudienceGroupDestination(group_id=group.id, destination_id=theirs))
            event_id = await make_event(sf, tenant)
            s.add(EventGroup(event_id=event_id, group_id=group.id))
            await s.commit()
            group_id = group.id
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, AT)
        assert sorted(c[1] for c in fake.calls if c[0] == "send") == ["mine", "theirs"]
        assert mine

        # Removing a member removes it from pending deliveries on the next sync.
        later = await make_event(sf, tenant, name="Later", anchor=datetime(2026, 10, 9).date())
        async with sf() as s:
            s.add(EventGroup(event_id=later, group_id=group_id))
            await s.commit()
        await sync(sf, later)
        async with sf() as s:
            await s.execute(AudienceGroupDestination.__table__.delete())
            await s.commit()
        await sync(sf, later)
        pending = [r for r in await deliveries(sf, later, kind="reminder") if r.status == "pending"]
        assert {r.destination_id for r in pending} == {mine}

    async def test_no_destination_records_why_nothing_was_sent(self, sf, fake, tenant):
        event_id = await make_event(sf, tenant)
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, AT)
        row = (await deliveries(sf, event_id, kind="reminder"))[0]
        assert row.status == "error" and row.detail == "No destination configured for this alliance"
        assert fake.count("send") == 0


class TestLeadership:
    async def test_a_leadership_event_posts_only_to_leadership_only_destinations(self, sf, fake, tenant):
        await add_destination(sf, tenant, "public")
        await add_destination(sf, tenant, "leaders", label="Leaders", leadership=True, default=True)
        event_id = await make_event(sf, tenant, leadership_only=True)
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, AT)
        assert [c[1] for c in fake.calls if c[0] == "send"] == ["leaders"]
        assert fake.count("create") == 0  # never a Scheduled Event

    async def test_an_ordinary_event_never_reaches_a_leadership_only_destination(self, sf, fake, tenant):
        await add_destination(sf, tenant, "public")
        leaders = await add_destination(sf, tenant, "leaders", label="Leaders", leadership=True)
        event_id = await make_event(sf, tenant)
        await change_destination(sf, event_id, leaders, True)  # even if asked for directly
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, AT)
        assert [c[1] for c in fake.calls if c[0] == "send"] == ["public"]

    async def test_a_leadership_event_with_no_leadership_destination_errors_clearly(self, sf, fake, tenant):
        await add_destination(sf, tenant, "public")
        event_id = await make_event(sf, tenant, leadership_only=True)
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, AT)
        row = (await deliveries(sf, event_id, kind="reminder"))[0]
        assert row.status == "error" and "leadership-only" in row.detail
        assert fake.count("send") == 0


class TestConflictsAfterSave:
    async def test_conflicting_overrides_send_the_base_message_once_with_an_explanation(self, sf, fake, tenant, second_tenant):
        event_id = await _shared_channel_setup(sf, tenant, second_tenant, message="Base text")
        async with sf() as s:
            row = (await s.execute(select(EventAlliance).where(
                EventAlliance.event_id == event_id, EventAlliance.tenant_id == second_tenant["id"]))).scalar_one()
            row.message_override = "NSR only wording"
            await s.commit()
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, AT)
        sends = [c for c in fake.calls if c[0] == "send"]
        assert len(sends) == 1 and sends[0][2] == "Base text"
        posted = next(r for r in await deliveries(sf, event_id, kind="reminder") if r.status == "posted")
        assert "Conflicting alliance messages" in posted.detail and "NSR" in posted.detail

    async def test_matching_overrides_are_not_a_conflict(self, sf, fake, tenant, second_tenant):
        event_id = await _shared_channel_setup(sf, tenant, second_tenant, message="Base text")
        async with sf() as s:
            for row in (await s.execute(select(EventAlliance).where(EventAlliance.event_id == event_id))).scalars():
                row.message_override = "Same for both"
            await s.commit()
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, AT)
        sends = [c for c in fake.calls if c[0] == "send"]
        assert len(sends) == 1 and sends[0][2] == "Same for both"
        posted = next(r for r in await deliveries(sf, event_id, kind="reminder") if r.status == "posted")
        assert not posted.detail

    async def test_an_occurrence_override_applies_to_everyone_without_conflict(self, sf, fake, tenant, second_tenant):
        from models.db import EventOccurrence
        event_id = await _shared_channel_setup(sf, tenant, second_tenant, message="Base text", interval=None)
        await sync(sf, event_id)
        async with sf() as s:
            occ = (await s.execute(select(EventOccurrence).where(EventOccurrence.event_id == event_id))).scalar_one()
            occ.message_override = "Tonight only"
            await s.commit()
        await run_delivery_tick(sf, fake, AT)
        assert [c[2] for c in fake.calls if c[0] == "send"] == ["Tonight only"]


class TestScheduledEvents:
    async def test_created_on_the_primary_server_only_and_shared_servers_get_one(self, sf, fake, tenant, second_tenant):
        # NSR moves onto MOD's server as its primary.
        from models.db import Tenant
        async with sf() as s:
            (await s.get(Tenant, second_tenant["id"])).server_id = tenant["server_id"]
            await s.commit()
        await add_destination(sf, tenant, "chan")
        event_id = await make_event(sf, tenant, audience=[tenant["id"], second_tenant["id"]], reminders=(), interval=None)
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, NOW)
        assert fake.count("create") == 1
        rows = await deliveries(sf, event_id, kind="discord_event")
        assert [r.status for r in rows] == ["posted", "posted"]
        assert rows[1].merged_into_id == rows[0].id and rows[0].merged_into_id is None

    async def test_a_secondary_server_never_gets_a_scheduled_event(self, sf, fake, tenant, second_tenant):
        from models.db import TenantSecondaryServer
        async with sf() as s:
            s.add(TenantSecondaryServer(tenant_id=tenant["id"], server_id=second_tenant["server_id"]))
            await s.commit()
        event_id = await make_event(sf, tenant, reminders=(), interval=None)
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, NOW)
        guilds = {c[1] for c in fake.calls if c[0] == "create"}
        assert guilds == {"test-guild-mod"}


class TestAudienceNamesInScheduledEventDescription:
    async def test_alliance_name_lists_every_alliance_on_the_guild(self, sf, fake, tenant, second_tenant):
        from models.db import Tenant
        async with sf() as s:
            (await s.get(Tenant, second_tenant["id"])).server_id = tenant["server_id"]
            await s.commit()
        event_id = await make_event(sf, tenant, audience=[tenant["id"], second_tenant["id"]], reminders=(),
                                    message="For {alliance_name}", interval=None)
        await sync(sf, event_id)
        await run_delivery_tick(sf, fake, NOW)
        assert [c[5] for c in fake.calls if c[0] == "create"] == ["For MOD, NSR"]


async def test_delivery_rows_snapshot_where_they_were_planned(sf, tenant):
    await add_destination(sf, tenant, "chan-snap")
    event_id = await make_event(sf, tenant)
    await sync(sf, event_id)
    row = (await deliveries(sf, event_id, kind="reminder"))[0]
    assert (row.guild_id, row.channel_id) == ("test-guild-mod", "chan-snap")
    async with sf() as s:
        assert (await s.execute(select(Delivery).where(Delivery.kind == "discord_event"))).scalars().first().channel_id == ""
