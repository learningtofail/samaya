"""Admin router package — split by concern.

Each submodule owns one slice of the admin surface; this file just wires
them together under the same `router` name main.py already imports, so
main.py needed no changes.
"""
from fastapi import APIRouter

from . import (
    ui, status, events, occurrences, scheduler_control,
    post_log, discord_config, discord_sync,
)

router = APIRouter()

router.include_router(ui.router)
router.include_router(status.router)
router.include_router(events.router)
router.include_router(occurrences.router)
router.include_router(scheduler_control.router)
router.include_router(post_log.router)
router.include_router(discord_config.router)
router.include_router(discord_sync.router)
