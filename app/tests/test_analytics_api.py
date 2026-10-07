"""Spec §85: the analytics endpoints. Scope, visibility and the numbers they return."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

import routers.admin.analytics as analytics_router
from models.db import Delivery, EventOccurrence, RedemptionResult, RedemptionRun
from tests.unified_helpers import make_event, sync

A = "/admin/api/analytics"
UTC = timezone.utc
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)  # a Wednesday
H = {"X-Tenant-Slug": "mod"}
ALL = {"X-Tenant-Slug": "*"}


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    monkeypatch.setattr(analytics_router, "_now", lambda: NOW)


async def _event(sf, tenant, **kw):
    kw.setdefault("anchor", NOW.date() + timedelta(days=1))  # Thursday
    event_id = await make_event(sf, tenant, **kw)
    await sync(sf, event_id, NOW)
    return event_id


async def _occurrences(sf, event_id):
    async with sf() as s:
        return list((await s.execute(select(EventOccurrence).where(EventOccurrence.event_id == event_id).order_by(EventOccurrence.start_datetime_utc))).scalars())


class TestHeatmap:
    async def test_counts_land_on_weekday_and_hour(self, client, sf, configured):
        await _event(sf, configured, interval=None)  # Thursday 19:00
        body = (await client.get(f"{A}/schedule-heatmap", headers=H)).json()
        assert body["grid"][3][19] == 1 and body["total"] == 1 and body["busiest"] == {"weekday": 3, "hour": 19, "count": 1}

    async def test_a_recurring_event_counts_every_occurrence_in_the_window(self, client, sf, configured):
        await _event(sf, configured, interval=7)
        assert (await client.get(f"{A}/schedule-heatmap?days=28", headers=H)).json()["grid"][3][19] >= 3

    async def test_cancelled_and_inactive_events_are_left_out(self, client, sf, configured):
        gone = await _event(sf, configured, interval=None, name="Gone")
        await _event(sf, configured, interval=None, name="Off", active=False)
        async with sf() as s:
            await s.execute(update(EventOccurrence).where(EventOccurrence.event_id == gone).values(status="cancelled"))
            await s.commit()
        assert (await client.get(f"{A}/schedule-heatmap", headers=H)).json()["total"] == 0

    async def test_a_moved_occurrence_counts_at_its_new_time(self, client, sf, configured):
        event_id = await _event(sf, configured, interval=None)
        async with sf() as s:
            await s.execute(update(EventOccurrence).where(EventOccurrence.event_id == event_id).values(
                start_datetime_utc_override=datetime(2026, 10, 9, 6, 0, tzinfo=UTC)))  # Friday 06:00
            await s.commit()
        grid = (await client.get(f"{A}/schedule-heatmap", headers=H)).json()["grid"]
        assert grid[4][6] == 1 and grid[3][19] == 0

    async def test_days_is_bounded(self, client, configured):
        assert (await client.get(f"{A}/schedule-heatmap?days=3", headers=H)).status_code == 422
        assert (await client.get(f"{A}/schedule-heatmap?days=90", headers=H)).status_code == 422

    async def test_an_empty_schedule_returns_zeros(self, client, configured):
        body = (await client.get(f"{A}/schedule-heatmap", headers=H)).json()
        assert body["total"] == 0 and body["busiest"] is None and len(body["grid"]) == 7


class TestOverlaps:
    async def test_two_events_sharing_time_are_reported(self, client, sf, configured):
        await _event(sf, configured, interval=None, name="A", start="19:00", duration=2.0)
        await _event(sf, configured, interval=None, name="B", start="20:00", duration=2.0)
        body = (await client.get(f"{A}/schedule-overlaps", headers=H)).json()
        assert body["count"] == 1 and body["overlaps"][0]["minutes"] == 60

    async def test_back_to_back_events_do_not_overlap(self, client, sf, configured):
        await _event(sf, configured, interval=None, name="A", start="19:00", duration=1.0)
        await _event(sf, configured, interval=None, name="B", start="20:00", duration=1.0)
        assert (await client.get(f"{A}/schedule-overlaps", headers=H)).json()["count"] == 0

    async def test_same_channel_is_flagged(self, client, sf, configured):
        await _event(sf, configured, interval=None, name="A", start="19:00", duration=2.0)
        await _event(sf, configured, interval=None, name="B", start="20:00", duration=2.0)
        found = (await client.get(f"{A}/schedule-overlaps", headers=H)).json()["overlaps"][0]
        assert found["shared_channels"] == ["chan-mod"]


class TestFreeSlots:
    async def test_slots_avoid_existing_events(self, client, sf, configured):
        await _event(sf, configured, interval=None, start="19:00", duration=2.0)
        slots = (await client.get(f"{A}/free-slots?duration=60&days=3", headers=H)).json()["slots"]
        assert len(slots) == 5
        busy_start, busy_end = datetime(2026, 10, 8, 19, tzinfo=UTC), datetime(2026, 10, 8, 21, tzinfo=UTC)
        for slot in slots:
            start = datetime.fromisoformat(slot["start"])
            assert not (start < busy_end and busy_start < start + timedelta(minutes=60))

    async def test_the_hours_window_is_respected(self, client, configured):
        slots = (await client.get(f"{A}/free-slots?duration=120&days=3&from_hour=18&to_hour=22", headers=H)).json()["slots"]
        assert slots and all(18 <= datetime.fromisoformat(s["start"]).hour < 22 and datetime.fromisoformat(s["end"]).hour <= 22 for s in slots)

    @pytest.mark.parametrize("query", ["from_hour=20&to_hour=20", "from_hour=20&to_hour=18", "duration=180&from_hour=18&to_hour=20", "duration=5", "duration=900", "days=0", "days=31"])
    async def test_bad_input_is_a_422(self, client, configured, query):
        assert (await client.get(f"{A}/free-slots?{query}", headers=H)).status_code == 422

    async def test_an_unknown_alliance_is_a_422_naming_it(self, client, configured):
        response = await client.get(f"{A}/free-slots?alliances=nope", headers=H)
        assert response.status_code == 422 and "nope" in response.json()["detail"]

    async def test_selecting_an_alliance_ignores_other_alliances_events(self, client, sf, configured, second_tenant):
        await _event(sf, second_tenant, interval=None, start="12:30", duration=1.0, anchor=NOW.date())
        both = (await client.get(f"{A}/free-slots?duration=60&days=1&from_hour=12&to_hour=14", headers=ALL)).json()["slots"]
        only_mod = (await client.get(f"{A}/free-slots?duration=60&days=1&from_hour=12&to_hour=14&alliances=mod", headers=ALL)).json()["slots"]
        assert both == [] and only_mod[0]["start"][11:16] == "12:00"


class TestDeliveryTrends:
    async def _post(self, sf, event_id, late_seconds=20):
        async with sf() as s:
            for d in (await s.execute(select(Delivery).join(EventOccurrence).where(EventOccurrence.event_id == event_id, Delivery.kind == "reminder"))).scalars():
                d.due_at_utc = NOW - timedelta(days=1, hours=1)
                d.status, d.posted_at_utc = "posted", d.due_at_utc + timedelta(seconds=late_seconds)
            await s.commit()

    async def test_lateness_and_counts(self, client, sf, configured):
        event_id = await _event(sf, configured, interval=None, reminders=(60,))
        await self._post(sf, event_id, late_seconds=20)
        body = (await client.get(f"{A}/delivery-trends?days=3", headers=H)).json()
        yesterday = next(d for d in body["days"] if d["date"] == "2026-10-06")
        assert (yesterday["due"], yesterday["posted"], yesterday["median_lateness_seconds"]) == (1, 1, 20)
        assert len(body["days"]) == 3

    async def test_merged_deliveries_count_once(self, client, sf, configured):
        event_id = await _event(sf, configured, interval=None, reminders=(60,))
        await self._post(sf, event_id)
        async with sf() as s:
            first = (await s.execute(select(Delivery).where(Delivery.kind == "reminder"))).scalars().first()
            s.add(Delivery(occurrence_id=first.occurrence_id, tenant_id=first.tenant_id, kind="reminder", reminder_minutes=60,
                           due_at_utc=first.due_at_utc, status="posted", posted_at_utc=first.posted_at_utc, channel_id="chan-mod", merged_into_id=first.id))
            await s.commit()
        assert sum(d["due"] for d in (await client.get(f"{A}/delivery-trends?days=3", headers=H)).json()["days"]) == 1

    async def test_by_destination_groups_per_channel(self, client, sf, configured):
        event_id = await _event(sf, configured, interval=None, reminders=(60,))
        await self._post(sf, event_id)
        body = (await client.get(f"{A}/delivery-trends?days=3&by=destination", headers=H)).json()
        assert len(body["destinations"]) == 1 and "Notifications" in body["destinations"][0]["destination"]

    async def test_bad_group_is_a_422(self, client, configured):
        assert (await client.get(f"{A}/delivery-trends?by=player", headers=H)).status_code == 422

    async def test_no_data_gives_a_flat_series(self, client, configured):
        body = (await client.get(f"{A}/delivery-trends?days=5", headers=H)).json()
        assert len(body["days"]) == 5 and all(d["due"] == 0 for d in body["days"])


async def _run(sf, tenant, code, outcomes, second=None):
    async with sf() as s:
        run = RedemptionRun(kingdom_id=tenant["kingdom_id"], code=code, status="done", created_at=NOW)
        s.add(run)
        await s.flush()
        for i, (t, status) in enumerate(outcomes):
            s.add(RedemptionResult(run_id=run.id, tenant_id=t["id"], fid=str(1000 + i), kid=138, status=status))
        await s.commit()
        return run.id


class TestCoverage:
    async def test_counts_per_alliance_never_per_player(self, client, sf, configured, second_tenant):
        await _run(sf, configured, "CODE1", [(configured, "SUCCESS"), (configured, "SUCCESS"), (configured, "RECEIVED"), (second_tenant, "USER INFO ERROR")])
        body = (await client.get(f"{A}/redemption-coverage", headers=ALL)).json()
        mod = next(a for a in body["runs"][0]["alliances"] if a["alliance"] == "MOD")
        assert (mod["players"], mod["redeemed"], mod["already"]) == (3, 2, 1) and mod["coverage_percent"] == 100
        assert "1000" not in str(body)

    async def test_no_runs_is_an_empty_list(self, client, configured):
        assert (await client.get(f"{A}/redemption-coverage", headers=H)).json() == {"runs": []}

    async def test_newest_run_first_and_limited(self, client, sf, configured):
        for i in range(3):
            await _run(sf, configured, f"C{i}", [(configured, "SUCCESS")])
        runs = (await client.get(f"{A}/redemption-coverage?runs=2", headers=H)).json()["runs"]
        assert [r["code"] for r in runs] == ["C2", "C1"]


class TestPermissions:
    async def test_a_viewer_can_read_everything(self, make_user_and_client, sf, configured):
        await _event(sf, configured, interval=None)
        viewer, _ = await make_user_and_client(tenant_grants=[(configured["id"], "viewer")])
        for path in ("schedule-heatmap", "schedule-overlaps", "free-slots", "delivery-trends", "redemption-coverage"):
            assert (await viewer.get(f"{A}/{path}", headers=H)).status_code == 200, path

    async def test_an_unreachable_alliance_never_appears(self, make_user_and_client, sf, configured, second_tenant):
        await _event(sf, second_tenant, interval=None, name="Secret NSR")
        client, _ = await make_user_and_client(tenant_grants=[(configured["id"], "owner")])
        assert (await client.get(f"{A}/schedule-heatmap", headers=ALL)).json()["total"] == 0
        assert (await client.get(f"{A}/schedule-heatmap", headers={"X-Tenant-Slug": "nsr"})).status_code in (403, 404)

    async def test_star_equals_the_union_of_reachable_alliances(self, client, sf, configured, second_tenant):
        await _event(sf, configured, interval=None, name="A")
        await _event(sf, second_tenant, interval=None, name="B", start="08:00")
        total = lambda r: r.json()["total"]  # noqa: E731
        both = total(await client.get(f"{A}/schedule-heatmap", headers=ALL))
        assert both == total(await client.get(f"{A}/schedule-heatmap", headers=H)) + total(await client.get(f"{A}/schedule-heatmap", headers={"X-Tenant-Slug": "nsr"}))

    async def test_a_signed_out_caller_gets_nothing(self, client_no_session, configured):
        assert (await client_no_session.get(f"{A}/schedule-heatmap", headers=H)).status_code in (401, 403)

    async def test_nothing_is_exposed_on_public_routes(self, client_no_session, configured):
        for path in ("/api/analytics/schedule-heatmap", "/api/events/analytics"):
            assert (await client_no_session.get(path)).status_code in (404, 405, 422)
