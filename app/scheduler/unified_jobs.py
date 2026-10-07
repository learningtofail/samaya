"""APScheduler entry points for the unified delivery engine (spec §66.4).
Thin wrappers: the logic and its tests live in services/event_engine.py."""
import logging
from datetime import datetime, timezone

import httpx

from models import AsyncSessionLocal
from services import discord_api, giftcode_client
from services.event_engine import run_delivery_tick, run_generation
from services.giftcode_engine import prune_redemptions, run_tick
from services.schedule_board import run_board_refresh
from services.time_poll import prune_polls, run_poll_tick

logger = logging.getLogger(__name__)


async def delivery_tick_job() -> None:
    try:
        counts = await run_delivery_tick(AsyncSessionLocal, discord_api)
        if counts:
            logger.info("delivery tick: %s", counts)
    except Exception:
        logger.exception("delivery tick failed")


async def board_tick_job() -> None:
    """Spec §82: edits each schedule board whose text changed. Separate from the delivery tick so a board
    problem can never delay a reminder."""
    try:
        counts = await run_board_refresh(AsyncSessionLocal, discord_api)
        changed = {k: v for k, v in counts.items() if k not in ("unchanged", "off", "skipped")}
        if changed:
            logger.info("schedule boards: %s", changed)
    except Exception:
        logger.exception("schedule board tick failed")


async def poll_tick_job() -> None:
    """Spec §84: closes expired polls and edits poll messages whose tally changed. Separate from the delivery
    tick so a poll problem can never delay a reminder."""
    try:
        counts = await run_poll_tick(AsyncSessionLocal, discord_api, datetime.now(timezone.utc))
        if any(counts.values()):
            logger.info("time polls: %s", counts)
    except Exception:
        logger.exception("time poll tick failed")


async def generation_job() -> None:
    try:
        logger.info("generation: synced %s events", await run_generation(AsyncSessionLocal))
    except Exception:
        logger.exception("generation failed")
    try:
        pruned = await prune_redemptions(AsyncSessionLocal, datetime.now(timezone.utc))
        if pruned:
            logger.info("retention: removed %s old gift code runs", pruned)
    except Exception:
        logger.exception("gift code retention failed")
    try:
        pruned = await prune_polls(AsyncSessionLocal, datetime.now(timezone.utc))
        if pruned:
            logger.info("retention: removed %s old time polls", pruned)
    except Exception:
        logger.exception("time poll retention failed")


async def giftcode_tick_job() -> None:
    """Spec §79.5. Registered only when KS_GIFTCODE_SIGN_KEY is set. Player IDs never reach the log."""
    if not giftcode_client.is_configured():
        return
    try:
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as http:
            counts = await run_tick(AsyncSessionLocal, giftcode_client.build_client(http))
        if counts:
            logger.info("gift code tick: %s", counts)
    except Exception:
        logger.exception("gift code tick failed")
