"""Shared helpers for the gift code tests (spec §79): a scripted fake client, a
mutable clock and direct-to-database builders."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from models.db import Kingdom, Player, RedemptionResult, RedemptionRun
from services.giftcode_client import FRIENDLY, RedeemResult

START = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, start=START):
        self.t = start

    def now(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


class FakeGiftClient:
    """`script` maps a player ID to a list of outcome keys returned in order; the
    last one repeats. Anything unscripted answers SUCCESS."""

    def __init__(self, script=None, default="SUCCESS"):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.default = default
        self.calls: list[tuple[str, int, str]] = []

    async def redeem(self, fid, kid, code):
        self.calls.append((fid, kid, code))
        keys = self.script.get(fid)
        key = (keys.pop(0) if len(keys) > 1 else keys[0]) if keys else self.default
        return RedeemResult(key, FRIENDLY.get(key, key))

    def count(self, fid=None):
        return sum(1 for c in self.calls if fid is None or c[0] == fid)


async def nosleep(_seconds):
    return None


def ticker(clock, **overrides):
    """Keyword arguments for run_tick that never wait and read `clock`."""
    return {"now": clock.now, "sleep": nosleep, "rng": lambda: 0.0, **overrides}


async def set_game_number(sf, kingdom_id, number=138):
    async with sf() as s:
        (await s.get(Kingdom, kingdom_id)).game_number = number
        await s.commit()


async def add_players(sf, tenant, *fids, kid=None):
    async with sf() as s:
        for fid in fids:
            s.add(Player(kingdom_id=tenant["kingdom_id"], tenant_id=tenant["id"], fid=str(fid), kid=kid))
        await s.commit()


async def results(sf, run_id):
    async with sf() as s:
        rows = (await s.execute(select(RedemptionResult).where(RedemptionResult.run_id == run_id).order_by(RedemptionResult.id))).scalars().all()
        return {r.fid: r for r in rows}


async def get_run(sf, run_id):
    async with sf() as s:
        return await s.get(RedemptionRun, run_id)
