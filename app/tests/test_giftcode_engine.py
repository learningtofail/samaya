"""Spec §79.2 to §79.4: creating a run and advancing it. A scripted fake client, a
mutable clock and no sleeping, so cooldowns and crashes are exercised without waiting."""
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from models.db import RedemptionResult, RedemptionRun
from services import giftcode_engine as engine
from services.giftcode_engine import RunRefused, create_run, run_tick
from tests.giftcode_helpers import START, Clock, FakeGiftClient, add_players, get_run, results, set_game_number, ticker

CODE = "KSTEST150"


async def _start(sf, tenant, code=CODE, tenants=None):
    async with sf() as s:
        from models.db import Kingdom
        kingdom = await s.get(Kingdom, tenant["kingdom_id"])
        run = await create_run(s, kingdom=kingdom, tenant_ids=tenants or [tenant["id"]], raw_code=code, user_id=None, now=START)
        await s.commit()
        return run.id


class TestCreateRun:
    async def test_several_alliances_are_one_run_and_the_kingdom_number_is_used(self, sf, tenant, second_tenant):
        await set_game_number(sf, tenant["kingdom_id"], 138)
        await add_players(sf, tenant, 11111111, 22222222)
        await add_players(sf, second_tenant, 33333333)
        rows = await results(sf, await _start(sf, tenant, tenants=[tenant["id"], second_tenant["id"]]))
        assert sorted(rows) == ["11111111", "22222222", "33333333"]
        assert {r.kid for r in rows.values()} == {138}
        assert {f: r.tenant_id for f, r in rows.items()}["33333333"] == second_tenant["id"]

    async def test_a_player_cannot_be_on_two_rosters(self, sf, tenant, second_tenant):
        from sqlalchemy.exc import IntegrityError
        await add_players(sf, tenant, 11111111)
        with pytest.raises(IntegrityError):
            await add_players(sf, second_tenant, 11111111)

    async def test_a_player_kingdom_overrides_the_game_number(self, sf, tenant):
        await set_game_number(sf, tenant["kingdom_id"], 138)
        await add_players(sf, tenant, 11111111, kid=245)
        assert (await results(sf, await _start(sf, tenant)))["11111111"].kid == 245

    async def test_no_resolvable_kingdom_is_refused_before_any_request(self, sf, tenant):
        await add_players(sf, tenant, 11111111)
        with pytest.raises(RunRefused) as e:
            await _start(sf, tenant)
        assert e.value.status == 422 and "11111111" in str(e.value)

    async def test_an_empty_roster_is_refused(self, sf, tenant):
        await set_game_number(sf, tenant["kingdom_id"])
        with pytest.raises(RunRefused):
            await _start(sf, tenant)

    async def test_one_active_run_at_a_time(self, sf, tenant):
        await set_game_number(sf, tenant["kingdom_id"])
        await add_players(sf, tenant, 11111111)
        await _start(sf, tenant)
        with pytest.raises(RunRefused) as e:
            await _start(sf, tenant, code="OTHER123")
        assert e.value.status == 409

    @pytest.mark.parametrize("raw", ["", "ab", "has space", "x" * 41, "semi;colon"])
    async def test_a_bad_code_is_refused(self, sf, tenant, raw):
        await set_game_number(sf, tenant["kingdom_id"])
        await add_players(sf, tenant, 11111111)
        with pytest.raises(RunRefused):
            await _start(sf, tenant, code=raw)

    async def test_invisible_characters_are_removed_from_a_pasted_code(self, sf, tenant):
        await set_game_number(sf, tenant["kingdom_id"])
        await add_players(sf, tenant, 11111111)
        run = await get_run(sf, await _start(sf, tenant, code="​KSTEST150﻿ "))
        assert run.code == "KSTEST150"

    async def test_players_redeemed_for_this_code_earlier_are_not_asked_again(self, sf, tenant):
        await set_game_number(sf, tenant["kingdom_id"])
        await add_players(sf, tenant, 11111111, 22222222)
        clock, client = Clock(), FakeGiftClient()
        first = await _start(sf, tenant)
        await run_tick(sf, client, **ticker(clock))
        assert (await get_run(sf, first)).status == "done"
        await add_players(sf, tenant, 33333333)
        second = await _start(sf, tenant)
        assert (await results(sf, second))["11111111"].status == "RECEIVED"
        await run_tick(sf, client, **ticker(clock))
        assert client.count("11111111") == 1 and client.count("33333333") == 1

    async def test_a_run_with_nobody_left_is_done_at_once(self, sf, tenant):
        await set_game_number(sf, tenant["kingdom_id"])
        await add_players(sf, tenant, 11111111)
        first = await _start(sf, tenant)
        await run_tick(sf, FakeGiftClient(), **ticker(Clock()))
        second = await _start(sf, tenant)
        assert (await get_run(sf, second)).status == "done" and first != second


class TestTick:
    async def _ready(self, sf, tenant, *fids):
        await set_game_number(sf, tenant["kingdom_id"])
        await add_players(sf, tenant, *fids)
        return await _start(sf, tenant)

    async def test_everyone_is_redeemed_and_the_run_finishes(self, sf, tenant):
        run_id = await self._ready(sf, tenant, 11111111, 22222222, 33333333)
        client = FakeGiftClient(script={"22222222": ["RECEIVED"]})
        await run_tick(sf, client, **ticker(Clock()))
        run = await get_run(sf, run_id)
        rows = await results(sf, run_id)
        assert run.status == "done" and run.finished_at is not None
        assert {f: r.status for f, r in rows.items()} == {"11111111": "SUCCESS", "22222222": "RECEIVED", "33333333": "SUCCESS"}
        assert client.calls[0] == ("11111111", 138, CODE)

    async def test_a_wrong_kingdom_is_final_and_the_run_carries_on(self, sf, tenant):
        run_id = await self._ready(sf, tenant, 11111111, 22222222)
        await run_tick(sf, FakeGiftClient(script={"11111111": ["USER INFO ERROR"]}), **ticker(Clock()))
        rows = await results(sf, run_id)
        assert rows["11111111"].status == "USER INFO ERROR" and rows["22222222"].status == "SUCCESS"
        assert (await get_run(sf, run_id)).status == "done"

    @pytest.mark.parametrize("key, reason", [("TIME ERROR", "code_expired"), ("CDK NOT FOUND", "code_invalid"), ("USED", "claim_limit")])
    async def test_a_dead_code_stops_the_whole_run(self, sf, tenant, key, reason):
        run_id = await self._ready(sf, tenant, 11111111, 22222222, 33333333)
        client = FakeGiftClient(script={"11111111": ["SUCCESS"], "22222222": [key]})
        await run_tick(sf, client, **ticker(Clock()))
        run, rows = await get_run(sf, run_id), await results(sf, run_id)
        assert (run.status, run.stop_reason) == ("stopped", reason)
        assert rows["22222222"].status == key and rows["33333333"].status == "SKIPPED"
        assert client.count("33333333") == 0

    async def test_a_rate_limited_player_is_parked_not_hammered(self, sf, tenant):
        run_id = await self._ready(sf, tenant, 11111111, 22222222)
        client, clock = FakeGiftClient(script={"11111111": ["TOO FREQUENT", "SUCCESS"]}), Clock()
        await run_tick(sf, client, **ticker(clock))
        rows = await results(sf, run_id)
        assert rows["11111111"].status == "cooling" and rows["22222222"].status == "SUCCESS"
        assert client.count("11111111") == 1
        await run_tick(sf, client, **ticker(clock))           # still inside the 60 seconds
        assert client.count("11111111") == 1
        assert (await get_run(sf, run_id)).status == "running"
        clock.advance(engine.COOLDOWN_SECONDS + 1)
        await run_tick(sf, client, **ticker(clock))
        assert (await results(sf, run_id))["11111111"].status == "SUCCESS"
        assert (await get_run(sf, run_id)).status == "done"

    async def test_a_player_who_stays_rate_limited_is_given_up_on_after_three_cooldowns(self, sf, tenant):
        run_id = await self._ready(sf, tenant, 11111111)
        client, clock = FakeGiftClient(script={"11111111": ["TOO FREQUENT"]}), Clock()
        for _ in range(engine.MAX_COOLDOWNS + 1):
            await run_tick(sf, client, **ticker(clock))
            clock.advance(engine.COOLDOWN_SECONDS + 1)
        row = (await results(sf, run_id))["11111111"]
        assert row.status == "TOO FREQUENT" and row.cooldowns == engine.MAX_COOLDOWNS + 1
        assert (await get_run(sf, run_id)).status == "done"

    async def test_a_server_retry_request_is_tried_three_times_then_recorded(self, sf, tenant):
        run_id = await self._ready(sf, tenant, 11111111)
        client, clock = FakeGiftClient(script={"11111111": ["TIMEOUT RETRY"]}), Clock()
        for _ in range(5):
            await run_tick(sf, client, **ticker(clock))
            clock.advance(10)
        assert client.count("11111111") == engine.MAX_ATTEMPTS
        assert (await results(sf, run_id))["11111111"].status == "TIMEOUT RETRY"

    async def test_ten_unreachable_players_in_a_row_stop_the_run(self, sf, tenant):
        run_id = await self._ready(sf, tenant, *range(10000001, 10000013))
        client = FakeGiftClient(default="TRANSPORT")
        await run_tick(sf, client, **ticker(Clock()))
        run = await get_run(sf, run_id)
        assert (run.status, run.stop_reason) == ("stopped", "unreachable")
        assert client.count() == engine.UNREACHABLE_LIMIT

    async def test_a_success_breaks_the_unreachable_streak(self, sf, tenant):
        script = {str(10000001 + i): ["SUCCESS" if i == 5 else "TRANSPORT"] for i in range(12)}
        run_id = await self._ready(sf, tenant, *range(10000001, 10000013))
        await run_tick(sf, FakeGiftClient(script=script), **ticker(Clock()))
        assert (await get_run(sf, run_id)).stop_reason is None

    @pytest.mark.parametrize("key", ["SIGN ERROR", "NOT LOGIN", "UNKNOWN"])
    async def test_three_suspicious_answers_in_a_row_mean_the_api_changed(self, sf, tenant, key):
        run_id = await self._ready(sf, tenant, 11111111, 22222222, 33333333, 44444444)
        client = FakeGiftClient(default=key)
        await run_tick(sf, client, **ticker(Clock()))
        run, rows = await get_run(sf, run_id), await results(sf, run_id)
        assert (run.status, run.stop_reason) == ("stopped", "api_changed")
        assert client.count() == engine.DRIFT_LIMIT and rows["44444444"].status == "SKIPPED"

    async def test_a_refusal_stops_the_run_at_once(self, sf, tenant):
        run_id = await self._ready(sf, tenant, 11111111, 22222222)
        client = FakeGiftClient(default="BLOCKED")
        await run_tick(sf, client, **ticker(Clock()))
        assert (await get_run(sf, run_id)).stop_reason == "blocked" and client.count() == 1

    async def test_a_crash_leaves_a_claim_that_is_recovered_after_five_minutes(self, sf, tenant):
        run_id = await self._ready(sf, tenant, 11111111)
        clock, client = Clock(), FakeGiftClient()
        async with sf() as s:
            await s.execute(update(RedemptionRun).values(status="running", started_at=START))
            await s.execute(update(RedemptionResult).values(status="in_flight", attempts=1, updated_at=START))
            await s.commit()
        await run_tick(sf, client, **ticker(clock))
        assert client.count() == 0                                # claim is still fresh
        clock.advance(6 * 60)
        out = await run_tick(sf, client, **ticker(clock))
        assert out["recovered"] == 1 and (await results(sf, run_id))["11111111"].status == "SUCCESS"

    async def test_the_tick_yields_at_its_deadline_and_the_next_tick_resumes(self, sf, tenant):
        run_id = await self._ready(sf, tenant, 11111111, 22222222, 33333333)
        client, clock = FakeGiftClient(), Clock()
        first = await run_tick(sf, client, **ticker(clock, deadline_seconds=0))
        assert first["processed"] == 1 and (await get_run(sf, run_id)).status == "running"
        await run_tick(sf, client, **ticker(clock))
        assert client.count() == 3 and (await get_run(sf, run_id)).status == "done"

    async def test_cancel_skips_the_rest_and_a_late_answer_is_ignored(self, sf, tenant):
        run_id = await self._ready(sf, tenant, 11111111, 22222222)
        async with sf() as s:
            run = await s.get(RedemptionRun, run_id)
            await engine.cancel_run(s, run, START)
            await s.commit()
        client = FakeGiftClient()
        assert await run_tick(sf, client, **ticker(Clock())) == {}
        assert client.count() == 0
        assert {r.status for r in (await results(sf, run_id)).values()} == {"CANCELLED"}

    async def test_no_active_run_is_a_quiet_tick(self, sf, tenant):
        assert await run_tick(sf, FakeGiftClient(), **ticker(Clock())) == {}


class TestRetention:
    async def test_finished_runs_older_than_90_days_are_removed_with_their_results(self, sf, tenant):
        await set_game_number(sf, tenant["kingdom_id"])
        await add_players(sf, tenant, 11111111)
        run_id = await _start(sf, tenant)
        await run_tick(sf, FakeGiftClient(), **ticker(Clock()))
        assert await engine.prune_redemptions(sf, START + timedelta(days=30)) == 0
        assert await engine.prune_redemptions(sf, START + timedelta(days=91)) == 1
        async with sf() as s:
            assert (await s.execute(select(RedemptionResult).where(RedemptionResult.run_id == run_id))).first() is None

    async def test_an_active_run_is_never_pruned(self, sf, tenant):
        await set_game_number(sf, tenant["kingdom_id"])
        await add_players(sf, tenant, 11111111)
        await _start(sf, tenant)
        assert await engine.prune_redemptions(sf, START + timedelta(days=400)) == 0


def test_outcome_groups_cover_every_state():
    counts = engine.summarize({"SUCCESS": 3, "RECEIVED": 2, "USER INFO ERROR": 1, "STOVE_LV ERROR": 1,
                               "TRANSPORT": 1, "pending": 2, "SKIPPED": 4, "TIME ERROR": 1})
    assert counts == {"redeemed": 3, "already": 2, "wrong_kingdom": 1, "requirement": 1, "other": 1,
                      "waiting": 2, "skipped": 4, "code": 1, "total": 15}
