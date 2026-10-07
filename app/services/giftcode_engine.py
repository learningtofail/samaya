"""Gift code redemption runs (spec §79). A run is database state and a tick job
advances it, so a restart loses nothing. Redemption is idempotent on the game's
side (`RECEIVED`), so a row left `in_flight` by a crash is simply retried.

The engine takes an injectable client, clock, sleep and random source, and
opens its own sessions (closing each before the network call), so tests drive it
with a fake client and never wait.
"""
import asyncio
import random
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import Kingdom, Player, RedemptionResult, RedemptionRun
from services.giftcode_client import DRIFT_KEYS, FATAL_KEYS, FRIENDLY, GiftcodeClient

COOLDOWN_SECONDS = 60
MAX_COOLDOWNS = 3
MAX_ATTEMPTS = 3            # server-requested retries per player
STALE_CLAIM = timedelta(minutes=5)
DRIFT_LIMIT = 3
UNREACHABLE_LIMIT = 10
TICK_DEADLINE_SECONDS = 50.0
DELAY_SECONDS = 1.0
JITTER_SECONDS = 0.5
RETENTION_DAYS = 90

ACTIVE_RUN = ("queued", "running")
WORKING = ("pending", "in_flight", "cooling")
SUCCESS_LIKE = ("SUCCESS", "RECEIVED")

_CODE_RE = re.compile(r"^[A-Za-z0-9]{3,40}$")
_INVISIBLE_RE = re.compile(r"[\x00-\x1f\x7f​-‏ -‮⁠﻿]")

REQUIREMENT_KEYS = ("STOVE_LV ERROR", "RECHARGE_MONEY ERROR", "RECHARGE_MONEY_VIP ERROR")


class RunRefused(Exception):
    """A run cannot start. `status` is the HTTP code the router answers with."""
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def normalize_code(raw: str) -> str:
    code = _INVISIBLE_RE.sub("", raw or "").strip()
    if not _CODE_RE.match(code):
        raise RunRefused("A gift code is 3 to 40 letters and digits")
    return code


def outcome_group(status: str) -> str:
    """The five numbers a leader reads, plus waiting, skipped, code and other."""
    if status == "SUCCESS":
        return "redeemed"
    if status == "RECEIVED":
        return "already"
    if status == "USER INFO ERROR":
        return "wrong_kingdom"
    if status in REQUIREMENT_KEYS:
        return "requirement"
    if status in WORKING:
        return "waiting"
    if status in ("SKIPPED", "CANCELLED"):
        return "skipped"
    if status in FATAL_KEYS:
        return "code"
    return "other"


def summarize(status_counts: dict[str, int]) -> dict[str, int]:
    out = {k: 0 for k in ("redeemed", "already", "wrong_kingdom", "requirement", "other", "waiting", "skipped", "code")}
    for status, n in status_counts.items():
        out[outcome_group(status)] += n
    out["total"] = sum(status_counts.values())
    return out


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def create_run(
    db: AsyncSession, *, kingdom: Kingdom, tenant_ids: list[int], raw_code: str, user_id: int | None, now: datetime,
) -> RedemptionRun:
    """Spec §79.6. Players on several rosters are redeemed once. Players already
    redeemed for this code in an earlier run are recorded as RECEIVED without a request."""
    code = normalize_code(raw_code)
    active = (await db.execute(
        select(RedemptionRun.id).where(RedemptionRun.kingdom_id == kingdom.id, RedemptionRun.status.in_(ACTIVE_RUN)).limit(1)
    )).first()
    if active:
        raise RunRefused("A gift code run is already in progress. Wait for it to finish or cancel it.", 409)

    players = (await db.execute(
        select(Player).where(Player.tenant_id.in_(tenant_ids), Player.kingdom_id == kingdom.id).order_by(Player.id)
    )).scalars().all()
    unique: dict[str, Player] = {}
    for p in players:
        unique.setdefault(p.fid, p)
    if not unique:
        raise RunRefused("Those alliances have no players yet. Add some on the Players tab.")

    missing = [p.fid for p in unique.values() if (p.kid or kingdom.game_number) is None]
    if missing:
        raise RunRefused(
            f"{len(missing)} player(s) have no kingdom number and the Kingdom has no game number set. "
            f"Set the game number in Setup or give those players a kingdom: {', '.join(missing[:20])}")

    already = set((await db.execute(
        select(RedemptionResult.fid).join(RedemptionRun, RedemptionRun.id == RedemptionResult.run_id)
        .where(RedemptionRun.kingdom_id == kingdom.id, RedemptionRun.code == code, RedemptionResult.status.in_(SUCCESS_LIKE))
    )).scalars().all())

    run = RedemptionRun(kingdom_id=kingdom.id, code=code, status="queued", created_by=user_id, created_at=now)
    db.add(run)
    await db.flush()
    pending = 0
    for p in unique.values():
        skip = p.fid in already
        pending += 0 if skip else 1
        db.add(RedemptionResult(
            run_id=run.id, tenant_id=p.tenant_id, player_id=p.id, fid=p.fid, kid=p.kid or kingdom.game_number,
            status="RECEIVED" if skip else "pending", attempts=0, cooldowns=0, updated_at=now,
            message="Already redeemed in an earlier run" if skip else None,
        ))
    if not pending:
        run.status, run.started_at, run.finished_at = "done", now, now
    return run


async def cancel_run(db: AsyncSession, run: RedemptionRun, now: datetime) -> None:
    await db.execute(
        update(RedemptionResult).where(RedemptionResult.run_id == run.id, RedemptionResult.status.in_(WORKING))
        .values(status="CANCELLED", message="Cancelled", updated_at=now))
    run.status, run.stop_reason, run.finished_at = "cancelled", "cancelled", now


async def _stop_run(db: AsyncSession, run_id: int, reason: str, now: datetime) -> None:
    await db.execute(
        update(RedemptionResult).where(RedemptionResult.run_id == run_id, RedemptionResult.status.in_(WORKING))
        .values(status="SKIPPED", message=f"Run stopped: {reason.replace('_', ' ')}", updated_at=now))
    run = await db.get(RedemptionRun, run_id)
    run.status, run.stop_reason, run.finished_at = "stopped", reason, now


async def recover_stale_claims(db: AsyncSession, run_id: int, now: datetime) -> int:
    result = await db.execute(
        update(RedemptionResult)
        .where(RedemptionResult.run_id == run_id, RedemptionResult.status == "in_flight",
               RedemptionResult.updated_at < now - STALE_CLAIM)
        .values(status="pending", updated_at=now))
    return result.rowcount or 0


async def _next_ready(db: AsyncSession, run_id: int, now: datetime) -> RedemptionResult | None:
    return (await db.execute(
        select(RedemptionResult)
        .where(RedemptionResult.run_id == run_id, RedemptionResult.status.in_(("pending", "cooling")),
               or_(RedemptionResult.next_attempt_at.is_(None), RedemptionResult.next_attempt_at <= now))
        .order_by(RedemptionResult.id).limit(1)
    )).scalar_one_or_none()


async def _streak(db: AsyncSession, run_id: int, length: int) -> list[str]:
    rows = (await db.execute(
        select(RedemptionResult.status)
        .where(RedemptionResult.run_id == run_id, RedemptionResult.status.notin_(WORKING + ("SKIPPED", "CANCELLED")))
        .order_by(RedemptionResult.updated_at.desc(), RedemptionResult.id.desc()).limit(length)
    )).scalars().all()
    return list(rows)


def _settle(row: RedemptionResult, key: str, message: str, now: datetime) -> str | None:
    """Apply one answer to a row. Returns a stop reason when the answer ends the whole run."""
    row.updated_at = now
    row.message = message
    if key == "TOO FREQUENT":
        row.cooldowns += 1
        if row.cooldowns <= MAX_COOLDOWNS:
            row.status, row.next_attempt_at = "cooling", now + timedelta(seconds=COOLDOWN_SECONDS)
            return None
    elif key == "TIMEOUT RETRY" and row.attempts < MAX_ATTEMPTS:
        row.status, row.next_attempt_at = "pending", now + timedelta(seconds=2 * row.attempts)
        return None
    row.status, row.next_attempt_at = key, None
    if key == "BLOCKED":
        return "blocked"
    return FATAL_KEYS.get(key)


async def run_tick(
    session_factory, client: GiftcodeClient, *, now: Callable[[], datetime] = _utcnow,
    deadline_seconds: float = TICK_DEADLINE_SECONDS, delay: float = DELAY_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, rng: Callable[[], float] = random.random,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict:
    """Advance the active run for up to `deadline_seconds`, then yield to the next tick.
    At least one player is processed per tick when one is ready."""
    async with session_factory() as db:
        run = (await db.execute(
            select(RedemptionRun).where(RedemptionRun.status.in_(ACTIVE_RUN)).order_by(RedemptionRun.id).limit(1)
        )).scalar_one_or_none()
        if run is None:
            return {}
        run_id = run.id
        recovered = await recover_stale_claims(db, run_id, now())
        if run.status == "queued":
            run.status, run.started_at = "running", now()
        await db.commit()

    started, processed, stopped = monotonic(), 0, None
    while True:
        async with session_factory() as db:
            run = await db.get(RedemptionRun, run_id)
            if run.status != "running":
                break
            row = await _next_ready(db, run_id, now())
            if row is None:
                left = (await db.execute(
                    select(func.count()).select_from(RedemptionResult)
                    .where(RedemptionResult.run_id == run_id, RedemptionResult.status.in_(WORKING)))).scalar_one()
                if left == 0:
                    run.status, run.finished_at = "done", now()
                    await db.commit()
                break
            row.status, row.attempts, row.updated_at = "in_flight", row.attempts + 1, now()
            row_id, fid, kid, code = row.id, row.fid, row.kid, run.code
            await db.commit()

        answer = await client.redeem(fid, kid, code)

        async with session_factory() as db:
            row = await db.get(RedemptionResult, row_id)
            if row is None or row.status != "in_flight":
                continue  # cancelled or recovered while the request was out
            stopped = _settle(row, answer.key, answer.message, now())
            await db.flush()
            if stopped is None and row.status not in WORKING:
                recent = await _streak(db, run_id, UNREACHABLE_LIMIT)
                if len(recent) == UNREACHABLE_LIMIT and all(s == "TRANSPORT" for s in recent):
                    stopped = "unreachable"
                elif len(recent) >= DRIFT_LIMIT and all(s in DRIFT_KEYS for s in recent[:DRIFT_LIMIT]):
                    stopped = "api_changed"
            if stopped:
                await _stop_run(db, run_id, stopped, now())
            await db.commit()
        processed += 1
        if stopped or monotonic() - started >= deadline_seconds:
            break
        await sleep(delay + rng() * JITTER_SECONDS)

    return {"run_id": run_id, "processed": processed, "stopped": stopped, "recovered": recovered}


async def prune_redemptions(session_factory, now: datetime, days: int = RETENTION_DAYS) -> int:
    """Retention (spec §79.3): finished runs older than `days` go, results first because the
    test database does not enforce foreign keys."""
    cutoff = now - timedelta(days=days)
    async with session_factory() as db:
        old = (await db.execute(
            select(RedemptionRun.id).where(
                RedemptionRun.status.notin_(ACTIVE_RUN), func.coalesce(RedemptionRun.finished_at, RedemptionRun.created_at) < cutoff)
        )).scalars().all()
        if old:
            await db.execute(delete(RedemptionResult).where(RedemptionResult.run_id.in_(old)))
            await db.execute(delete(RedemptionRun).where(RedemptionRun.id.in_(old)))
            await db.commit()
        return len(old)


__all__ = ["FRIENDLY", "RunRefused", "create_run", "cancel_run", "run_tick", "prune_redemptions", "summarize", "normalize_code"]
