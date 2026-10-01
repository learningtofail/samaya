"""APScheduler entry points for the unified delivery engine (spec §66.4).
Thin wrappers: the logic and its tests live in services/event_engine.py."""
import logging

from models import AsyncSessionLocal
from services import discord_api
from services.event_engine import run_delivery_tick, run_generation

logger = logging.getLogger(__name__)


async def delivery_tick_job() -> None:
    try:
        counts = await run_delivery_tick(AsyncSessionLocal, discord_api)
        if counts:
            logger.info("delivery tick: %s", counts)
    except Exception:
        logger.exception("delivery tick failed")


async def generation_job() -> None:
    try:
        logger.info("generation: synced %s events", await run_generation(AsyncSessionLocal))
    except Exception:
        logger.exception("generation failed")
